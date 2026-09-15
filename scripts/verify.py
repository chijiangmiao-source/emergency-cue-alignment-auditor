"""One-shot acceptance checks against a running alignment API.

Uses only the standard library so the runtime image needs no extra
dependencies.  Configure the target with ``API_BASE_URL`` (default
``http://localhost:8000``).  Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
HEALTH_TIMEOUT_S = float(os.environ.get("VERIFY_HEALTH_TIMEOUT_S", "60"))


class CheckFailed(AssertionError):
    pass


def _request(method: str, path: str, payload: object = None, raw: bytes | None = None):
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _align(payload):
    status, body = _request("POST", "/align", payload=payload)
    if status != 200:
        raise CheckFailed(f"expected 200, got {status}: {body}")
    return body


def _expect_equal(name: str, got, want) -> None:
    if got != want:
        raise CheckFailed(f"{name}\n  want: {want!r}\n  got:  {got!r}")


def check_health() -> None:
    status, body = _request("GET", "/healthz")
    _expect_equal("health status", status, 200)
    _expect_equal("health body", body, {"status": "ok"})


def check_compliant_exact() -> None:
    body = _align(
        {
            "planned": [
                {"code": "ADS1", "at_ms": 1000},
                {"code": "NEWS", "at_ms": 2000},
            ],
            "actual": [
                {"code": "ADS1", "at_ms": 1000},
                {"code": "NEWS", "at_ms": 2000},
            ],
        }
    )
    _expect_equal(
        "exact replay",
        body,
        {
            "compliant": True,
            "total_cost": 0,
            "pairs": [
                {
                    "op": "MATCH",
                    "cost": 0,
                    "code": "ADS1",
                    "planned_index": 0,
                    "actual_index": 0,
                    "planned_at_ms": 1000,
                    "actual_at_ms": 1000,
                    "drift_ms": 0,
                },
                {
                    "op": "MATCH",
                    "cost": 0,
                    "code": "NEWS",
                    "planned_index": 1,
                    "actual_index": 1,
                    "planned_at_ms": 2000,
                    "actual_at_ms": 2000,
                    "drift_ms": 0,
                },
            ],
            "first_defect": None,
        },
    )


def check_compliant_boundary_500() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1500}],
        }
    )
    _expect_equal("drift exactly 500 stays compliant", body["compliant"], True)
    _expect_equal("drift exactly 500 cost", body["total_cost"], 500)
    _expect_equal("drift exactly 500 defect", body["first_defect"], None)


def check_drift_501_is_defect() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1501}],
        }
    )
    _expect_equal("drift 501 compliant", body["compliant"], False)
    _expect_equal("drift 501 cost", body["total_cost"], 501)
    defect = body["first_defect"]
    _expect_equal("drift 501 defect code", defect["code"], "DRIFT")
    _expect_equal("drift 501 defect index", defect["pair_index"], 0)
    _expect_equal("drift 501 defect drift", defect["pair"]["drift_ms"], 501)


def check_match_gate_2000() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
        }
    )
    _expect_equal("drift 2000 matches", [p["op"] for p in body["pairs"]], ["MATCH"])
    _expect_equal("drift 2000 cost", body["total_cost"], 2000)
    _expect_equal("drift 2000 defect", body["first_defect"]["code"], "DRIFT")


def check_match_gate_2001() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3001}],
        }
    )
    _expect_equal(
        "drift 2001 cannot match",
        [p["op"] for p in body["pairs"]],
        ["DELETE", "INSERT"],
    )
    _expect_equal("drift 2001 cost", body["total_cost"], 5000)
    _expect_equal("drift 2001 first defect", body["first_defect"]["code"], "MISS")


def check_duplicate_code_tie_planned() -> None:
    # planned A@0, A@1000 vs actual A@500: both alignments cost 3000; the
    # lexicographic rule must keep MATCH first, so the leftmost planned item
    # wins the actual slot and the second planned item is deleted.
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
        }
    )
    _expect_equal(
        "duplicate planned codes resolve to a single path",
        body,
        {
            "compliant": False,
            "total_cost": 3000,
            "pairs": [
                {
                    "op": "MATCH",
                    "cost": 500,
                    "code": "A",
                    "planned_index": 0,
                    "actual_index": 0,
                    "planned_at_ms": 0,
                    "actual_at_ms": 500,
                    "drift_ms": 500,
                },
                {
                    "op": "DELETE",
                    "cost": 2500,
                    "code": "A",
                    "planned_index": 1,
                    "planned_at_ms": 1000,
                },
            ],
            "first_defect": {
                "code": "MISS",
                "pair_index": 1,
                "pair": {
                    "op": "DELETE",
                    "cost": 2500,
                    "code": "A",
                    "planned_index": 1,
                    "planned_at_ms": 1000,
                },
            },
        },
    )


def check_duplicate_code_tie_actual() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 500}],
            "actual": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
        }
    )
    _expect_equal(
        "duplicate actual codes resolve to a single path",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "INSERT"],
    )
    _expect_equal("duplicate actual cost", body["total_cost"], 3000)
    _expect_equal("duplicate actual defect", body["first_defect"]["code"], "EXTRA")


def check_delete_precedes_insert_on_tie() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "B", "at_ms": 0}],
        }
    )
    _expect_equal(
        "unmatchable items order DELETE before INSERT",
        [p["op"] for p in body["pairs"]],
        ["DELETE", "INSERT"],
    )
    _expect_equal("unmatchable cost", body["total_cost"], 5000)
    _expect_equal("unmatchable defect", body["first_defect"]["code"], "MISS")


def check_multi_equal_cost_paths() -> None:
    # Three minimal alignments cost 3500; only MATCH,MATCH,DELETE is the
    # lexicographically smallest operation string.
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            "actual": [
                {"code": "A", "at_ms": 500},
                {"code": "A", "at_ms": 1500},
            ],
        }
    )
    _expect_equal(
        "three-way tie picks MATCH,MATCH,DELETE",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "MATCH", "DELETE"],
    )
    _expect_equal("three-way tie cost", body["total_cost"], 3500)
    _expect_equal(
        "three-way tie pairs",
        [(p.get("planned_index"), p.get("actual_index")) for p in body["pairs"]],
        [(0, 0), (1, 1), (2, None)],
    )


def check_first_defect_is_leftmost() -> None:
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "B", "at_ms": 1000},
                {"code": "C", "at_ms": 2000},
            ],
            "actual": [
                {"code": "A", "at_ms": 100},
                {"code": "C", "at_ms": 2900},
                {"code": "D", "at_ms": 3000},
            ],
        }
    )
    _expect_equal(
        "mixed scenario ops",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "DELETE", "MATCH", "INSERT"],
    )
    _expect_equal("mixed scenario cost", body["total_cost"], 6000)
    defect = body["first_defect"]
    _expect_equal("leftmost defect is the DELETE, not the later DRIFT/INSERT",
                  defect["code"], "MISS")
    _expect_equal("leftmost defect index", defect["pair_index"], 1)


def check_empty_arrays() -> None:
    body = _align({"planned": [], "actual": []})
    _expect_equal(
        "empty alignment",
        body,
        {"compliant": True, "total_cost": 0, "pairs": [], "first_defect": None},
    )


def check_determinism() -> None:
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "A", "at_ms": 1000},
            {"code": "A", "at_ms": 2000},
        ],
        "actual": [
            {"code": "A", "at_ms": 500},
            {"code": "A", "at_ms": 1500},
        ],
    }
    first = _align(payload)
    for _ in range(3):
        _expect_equal("repeated calls must be identical", _align(payload), first)


def _expect_422(payload=None, raw: bytes | None = None, want_detail: str = ""):
    status, body = _request("POST", "/align", payload=payload, raw=raw)
    _expect_equal("validation status", status, 422)
    if body.get("error", {}).get("code") != "VALIDATION_FAILED":
        raise CheckFailed(f"unexpected error envelope: {body}")
    codes = [d["code"] for d in body["error"]["details"]]
    if want_detail and want_detail not in codes:
        raise CheckFailed(f"expected detail {want_detail} in {codes}")
    # The same request must always produce the identical body.
    again_status, again = _request("POST", "/align", payload=payload, raw=raw)
    _expect_equal("422 repeat status", again_status, 422)
    _expect_equal("422 body must be stable", again, body)
    return codes


def check_validation_codes() -> None:
    _expect_422(
        {"planned": [{"code": "bad", "at_ms": 0}], "actual": []},
        want_detail="INVALID_CODE",
    )
    _expect_422(
        {"planned": [{"code": "TOOLONGCODE1234567X", "at_ms": 0}], "actual": []},
        want_detail="INVALID_CODE",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": -1}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": True}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": 1.5}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {
            "planned": [
                {"code": "A", "at_ms": 1000},
                {"code": "B", "at_ms": 1000},
            ],
            "actual": [],
        },
        want_detail="NOT_STRICTLY_INCREASING",
    )
    _expect_422(
        {
            "planned": [],
            "actual": [
                {"code": "A", "at_ms": 2000},
                {"code": "B", "at_ms": 1999},
            ],
        },
        want_detail="NOT_STRICTLY_INCREASING",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": 0, "note": "x"}], "actual": []},
        want_detail="UNEXPECTED_FIELD",
    )
    _expect_422({"planned": [{"at_ms": 0}], "actual": []}, want_detail="MISSING_FIELD")
    _expect_422({"planned": {}, "actual": []}, want_detail="INVALID_TYPE")
    _expect_422({"actual": []}, want_detail="MISSING_FIELD")
    _expect_422(raw=b"not json", want_detail="INVALID_PAYLOAD")
    _expect_422(payload=[1, 2, 3], want_detail="INVALID_PAYLOAD")


def check_alternatives_unique_optimum_gap() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "A", "at_ms": 100}],
            "alternative_limit": 1,
        }
    )
    _expect_equal("unique optimum top cost", body["total_cost"], 100)
    _expect_equal(
        "unique optimum keys append alternatives last",
        list(body),
        ["compliant", "total_cost", "pairs", "first_defect", "alternatives"],
    )
    _expect_equal("unique optimum alternative count", len(body["alternatives"]), 1)
    alternative = body["alternatives"][0]
    _expect_equal(
        "unique optimum alternative keys",
        list(alternative),
        [
            "total_cost",
            "cost_gap",
            "pairs",
            "compliant",
            "first_defect",
            "first_divergence_index",
        ],
    )
    _expect_equal(
        "runner-up ops", [p["op"] for p in alternative["pairs"]], ["DELETE", "INSERT"]
    )
    _expect_equal("runner-up total cost", alternative["total_cost"], 5000)
    _expect_equal("runner-up positive gap", alternative["cost_gap"], 4900)
    if alternative["cost_gap"] <= 0:
        raise CheckFailed("unique optimum must have a positive cost gap")
    _expect_equal("runner-up divergence", alternative["first_divergence_index"], 0)
    _expect_equal("runner-up compliance", alternative["compliant"], False)
    _expect_equal(
        "runner-up first defect", alternative["first_defect"]["code"], "MISS"
    )


def check_alternatives_duplicate_code_fork() -> None:
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
            "alternative_limit": 1,
        }
    )
    _expect_equal(
        "duplicate code preferred ops",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "DELETE"],
    )
    _expect_equal("duplicate code alternative count", len(body["alternatives"]), 1)
    alternative = body["alternatives"][0]
    _expect_equal(
        "duplicate code fork ops",
        [p["op"] for p in alternative["pairs"]],
        ["DELETE", "MATCH"],
    )
    _expect_equal("duplicate code fork total", alternative["total_cost"], 3000)
    _expect_equal("duplicate code fork gap", alternative["cost_gap"], 0)
    _expect_equal(
        "duplicate code fork divergence", alternative["first_divergence_index"], 0
    )
    _expect_equal(
        "fork match uses later planned copy",
        alternative["pairs"][1]["planned_index"],
        1,
    )


def check_alternatives_shortage() -> None:
    body = _align({"planned": [], "actual": [], "alternative_limit": 20})
    _expect_equal("empty sequences alternatives", body["alternatives"], [])
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "A", "at_ms": 0}],
            "alternative_limit": 20,
        }
    )
    # MATCH, DELETE+INSERT and INSERT+DELETE are all the legal paths.
    _expect_equal("fewer paths than limit returns actual count",
                  len(body["alternatives"]), 2)
    paths = [tuple(p["op"] for p in body["pairs"])]
    paths += [
        tuple(p["op"] for p in alternative["pairs"])
        for alternative in body["alternatives"]
    ]
    if len(set(paths)) != len(paths):
        raise CheckFailed(f"duplicate alternative paths: {paths}")
    gaps = [alternative["cost_gap"] for alternative in body["alternatives"]]
    _expect_equal("gaps sorted relative to preferred", gaps, sorted(gaps))


def check_alternatives_legacy_byte_compatibility() -> None:
    raw = json.dumps(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "B", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 100}],
        }
    ).encode("utf-8")

    def raw_align() -> bytes:
        req = urllib.request.Request(
            BASE_URL + "/align",
            data=raw,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise CheckFailed(f"expected 200, got {resp.status}")
            return resp.read()

    text = raw_align()
    if b"alternatives" in text:
        raise CheckFailed("legacy response must not contain alternatives")
    for _ in range(2):
        if raw_align() != text:
            raise CheckFailed("legacy responses must be byte identical")


def check_alternative_limit_validation() -> None:
    for bad in (0, 21, -5, True, 1.5, "3", None):
        codes = _expect_422(
            {"planned": [], "actual": [], "alternative_limit": bad},
            want_detail="INVALID_ALTERNATIVE_LIMIT",
        )
        _expect_equal(f"limit {bad!r} single code", codes,
                      ["INVALID_ALTERNATIVE_LIMIT"])
    # Boundaries are accepted.
    for value in (1, 20):
        status, body = _request(
            "POST",
            "/align",
            payload={"planned": [], "actual": [], "alternative_limit": value},
        )
        _expect_equal(f"limit {value} status", status, 200)
        _expect_equal(f"limit {value} alternatives", body["alternatives"], [])


CHECKS = [
    ("health", check_health),
    ("compliant exact replay", check_compliant_exact),
    ("compliant at exactly 500ms drift", check_compliant_boundary_500),
    ("drift defect at 501ms", check_drift_501_is_defect),
    ("match gate at exactly 2000ms", check_match_gate_2000),
    ("match gate rejects 2001ms", check_match_gate_2001),
    ("duplicate planned codes tie", check_duplicate_code_tie_planned),
    ("duplicate actual codes tie", check_duplicate_code_tie_actual),
    ("DELETE before INSERT on tie", check_delete_precedes_insert_on_tie),
    ("multiple equal-cost paths", check_multi_equal_cost_paths),
    ("first defect is leftmost", check_first_defect_is_leftmost),
    ("empty arrays", check_empty_arrays),
    ("determinism across calls", check_determinism),
    ("alternatives unique optimum positive gap",
     check_alternatives_unique_optimum_gap),
    ("alternatives duplicate code zero-gap fork",
     check_alternatives_duplicate_code_fork),
    ("alternatives shortage returns real count", check_alternatives_shortage),
    ("alternatives legacy byte compatibility",
     check_alternatives_legacy_byte_compatibility),
    ("alternative_limit validation codes", check_alternative_limit_validation),
    ("validation machine codes", check_validation_codes),
]


def wait_for_api() -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/healthz", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def main() -> int:
    print(f"verify: targeting {BASE_URL}", flush=True)
    if not wait_for_api():
        print("verify: API did not become healthy in time", flush=True)
        return 1
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            print(f"FAIL {name}: {exc}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    total = len(CHECKS)
    print(f"verify: {total - failures}/{total} checks passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
