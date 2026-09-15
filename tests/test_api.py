"""HTTP-level tests: contract, boundaries, defects and 422 machine codes."""


def post(client, payload):
    return client.post("/align", json=payload)


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_compliant_exact_replay(client):
    resp = post(
        client,
        {
            "planned": [{"code": "ADS1", "at_ms": 1000}],
            "actual": [{"code": "ADS1", "at_ms": 1000}],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
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
            }
        ],
        "first_defect": None,
    }


def test_compliant_at_exactly_500ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1500}],
        },
    ).json()
    assert body["compliant"] is True
    assert body["total_cost"] == 500
    assert body["first_defect"] is None


def test_drift_defect_at_501ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1501}],
        },
    ).json()
    assert body["compliant"] is False
    assert body["total_cost"] == 501
    assert body["first_defect"]["code"] == "DRIFT"
    assert body["first_defect"]["pair_index"] == 0
    assert body["first_defect"]["pair"]["drift_ms"] == 501


def test_match_gate_at_exactly_2000ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH"]
    assert body["total_cost"] == 2000
    assert body["compliant"] is False
    assert body["first_defect"]["code"] == "DRIFT"


def test_match_gate_rejects_2001ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3001}],
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["DELETE", "INSERT"]
    assert body["total_cost"] == 5000
    assert body["first_defect"]["code"] == "MISS"


def test_empty_arrays_are_compliant(client):
    body = post(client, {"planned": [], "actual": []}).json()
    assert body == {
        "compliant": True,
        "total_cost": 0,
        "pairs": [],
        "first_defect": None,
    }


def test_missed_and_extra_items(client):
    body = post(
        client,
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
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "DELETE", "MATCH", "INSERT"]
    assert body["total_cost"] == 6000
    # The leftmost defect is the missed B, not the later drifted C or extra D.
    assert body["first_defect"]["code"] == "MISS"
    assert body["first_defect"]["pair_index"] == 1
    assert body["first_defect"]["pair"]["planned_index"] == 1


def test_response_is_deterministic(client):
    payload = {
        "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


def _assert_422(resp, *detail_codes):
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    codes = [d["code"] for d in body["error"]["details"]]
    for expected in detail_codes:
        assert expected in codes, codes
    for detail in body["error"]["details"]:
        assert set(detail) == {"code", "path", "message"}
    return body


def test_422_invalid_code(client):
    for bad in ["abc", "a", "", "CODE-1", "X" * 17, 123, None]:
        _assert_422(
            post(client, {"planned": [{"code": bad, "at_ms": 0}], "actual": []}),
            "INVALID_CODE",
        )


def test_422_valid_code_shapes(client):
    resp = post(
        client,
        {
            "planned": [{"code": "Z9" * 8, "at_ms": 0}],
            "actual": [{"code": "Z9" * 8, "at_ms": 0}],
        },
    )
    assert resp.status_code == 200


def test_422_invalid_at_ms(client):
    for bad in [-1, 1.5, "1000", True, False, None]:
        _assert_422(
            post(client, {"planned": [{"code": "A", "at_ms": bad}], "actual": []}),
            "INVALID_AT_MS",
        )


def test_422_not_strictly_increasing(client):
    body = _assert_422(
        post(
            client,
            {
                "planned": [
                    {"code": "A", "at_ms": 1000},
                    {"code": "B", "at_ms": 1000},
                ],
                "actual": [],
            },
        ),
        "NOT_STRICTLY_INCREASING",
    )
    assert body["error"]["details"][0]["path"] == "planned[1].at_ms"
    _assert_422(
        post(
            client,
            {
                "planned": [],
                "actual": [
                    {"code": "A", "at_ms": 2000},
                    {"code": "B", "at_ms": 1999},
                ],
            },
        ),
        "NOT_STRICTLY_INCREASING",
    )


def test_422_structure_errors(client):
    _assert_422(post(client, {"actual": []}), "MISSING_FIELD")
    _assert_422(post(client, {"planned": {}, "actual": []}), "INVALID_TYPE")
    _assert_422(post(client, {"planned": "nope", "actual": []}), "INVALID_TYPE")
    _assert_422(
        post(client, {"planned": [{"at_ms": 0}], "actual": []}), "MISSING_FIELD"
    )
    _assert_422(
        post(client, {"planned": [{"code": "A"}], "actual": []}), "MISSING_FIELD"
    )
    _assert_422(
        post(client, {"planned": [{"code": "A", "at_ms": 0, "x": 1}], "actual": []}),
        "UNEXPECTED_FIELD",
    )
    _assert_422(post(client, {"planned": [7], "actual": []}), "INVALID_TYPE")
    _assert_422(post(client, [1, 2, 3]), "INVALID_PAYLOAD")


def test_422_malformed_json(client):
    resp = client.post(
        "/align", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _assert_422(resp, "INVALID_PAYLOAD")


def test_422_body_is_stable(client):
    payload = {
        "planned": [{"code": "bad!", "at_ms": -3}],
        "actual": [{"code": "A", "at_ms": 5}, {"code": "B", "at_ms": 5}],
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


def test_422_collects_multiple_details(client):
    body = _assert_422(
        post(
            client,
            {
                "planned": [{"code": "ok", "at_ms": 0}, {"code": "A"}],
                "actual": [{"code": "B", "at_ms": -1}],
            },
        ),
        "INVALID_CODE",
        "MISSING_FIELD",
        "INVALID_AT_MS",
    )
    paths = [d["path"] for d in body["error"]["details"]]
    assert paths == ["planned[0].code", "planned[1].at_ms", "actual[0].at_ms"]


# --- alternative_limit ----------------------------------------------------


def test_omitted_alternative_limit_response_is_unchanged(client):
    payload = {
        "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    body = post(client, payload)
    assert body.status_code == 200
    assert "alternatives" not in body.json()
    # Byte-for-byte identical to an explicit legacy-style repetition, and the
    # top-level key order keeps first_defect last.
    assert body.json() == {
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
    }
    text = body.text
    assert list(body.json()) == ["compliant", "total_cost", "pairs", "first_defect"]
    assert text.index("pairs") < text.index("first_defect")
    assert "alternatives" not in text
    assert post(client, payload).text == text


def test_alternatives_shape_and_content(client):
    payload = {
        "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
        "actual": [{"code": "A", "at_ms": 500}],
        "alternative_limit": 1,
    }
    resp = post(client, payload)
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == [
        "compliant",
        "total_cost",
        "pairs",
        "first_defect",
        "alternatives",
    ]
    # limit=1 returns the preferred path plus its single equal-cost fork
    # (DELETE,MATCH), even though further more expensive paths exist.
    assert len(body["alternatives"]) == 1
    alternative = body["alternatives"][0]
    assert list(alternative) == [
        "total_cost",
        "cost_gap",
        "pairs",
        "compliant",
        "first_defect",
        "first_divergence_index",
    ]
    assert alternative["total_cost"] == 3000
    assert alternative["cost_gap"] == 0
    assert [p["op"] for p in alternative["pairs"]] == ["DELETE", "MATCH"]
    assert alternative["pairs"][0] == {
        "op": "DELETE",
        "cost": 2500,
        "code": "A",
        "planned_index": 0,
        "planned_at_ms": 0,
    }
    assert alternative["pairs"][1]["planned_index"] == 1
    assert alternative["pairs"][1]["actual_index"] == 0
    assert alternative["compliant"] is False
    assert alternative["first_defect"] == {
        "code": "MISS",
        "pair_index": 0,
        "pair": alternative["pairs"][0],
    }
    assert alternative["first_divergence_index"] == 0
    assert post(client, payload).text == resp.text


def test_unique_optimum_reports_positive_gap(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "A", "at_ms": 100}],
            "alternative_limit": 1,
        },
    ).json()
    assert body["total_cost"] == 100
    assert len(body["alternatives"]) == 1
    alternative = body["alternatives"][0]
    assert [p["op"] for p in alternative["pairs"]] == ["DELETE", "INSERT"]
    assert alternative["total_cost"] == 5000
    assert alternative["cost_gap"] == 4900
    assert alternative["cost_gap"] > 0
    assert alternative["first_divergence_index"] == 0
    assert alternative["first_defect"]["code"] == "MISS"


def test_alternative_paths_are_unique(client):
    body = post(
        client,
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            "actual": [{"code": "A", "at_ms": 500}, {"code": "A", "at_ms": 1500}],
            "alternative_limit": 20,
        },
    ).json()
    paths = [tuple(pair["op"] for pair in body["pairs"])]
    paths += [
        tuple(pair["op"] for pair in alternative["pairs"])
        for alternative in body["alternatives"]
    ]
    assert len(paths) == len(set(paths))
    gaps = [alternative["cost_gap"] for alternative in body["alternatives"]]
    assert gaps == sorted(gaps)
    assert all(gap >= 0 for gap in gaps)


def test_alternative_limit_boundaries_accepted(client):
    payload = {"planned": [], "actual": []}
    for value in (1, 20):
        resp = post(client, {**payload, "alternative_limit": value})
        assert resp.status_code == 200, value
        assert resp.json()["alternatives"] == []


def test_422_alternative_limit_values(client):
    for bad in [0, 21, -1, 1.0, 1.5, "1", True, False, None, [], {}, 100]:
        body = _assert_422(
            post(
                client,
                {
                    "planned": [],
                    "actual": [],
                    "alternative_limit": bad,
                },
            ),
            "INVALID_ALTERNATIVE_LIMIT",
        )
        detail = body["error"]["details"][0]
        assert detail["code"] == "INVALID_ALTERNATIVE_LIMIT"
        assert detail["path"] == "alternative_limit"


def test_422_alternative_limit_does_not_suppress_other_errors(client):
    body = _assert_422(
        post(
            client,
            {
                "planned": "nope",
                "actual": [],
                "alternative_limit": 0,
            },
        ),
        "INVALID_TYPE",
        "INVALID_ALTERNATIVE_LIMIT",
    )
    codes = [d["code"] for d in body["error"]["details"]]
    # Structural/field errors keep their ordering ahead of nothing: the
    # alternative_limit detail is simply present and deterministic.
    assert codes == ["INVALID_TYPE", "INVALID_ALTERNATIVE_LIMIT"]


def test_alternatives_are_deterministic(client):
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "B", "at_ms": 1000},
        ],
        "actual": [{"code": "A", "at_ms": 200}, {"code": "B", "at_ms": 3200}],
        "alternative_limit": 10,
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1
