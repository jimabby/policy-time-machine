"""Regression checks for replay isolation and the import-to-review workflow."""
from __future__ import annotations

import json
import shutil
from datetime import datetime

import pytest

from ptm import cache, config, diff, ingest, proposal, provenance, replay, report, store
from ptm.models import Case, Verdict


def row(case_id="import-1", **changes):
    return {"case_id": case_id, "subject_id": "import-subject", "decided_at": "2025-01-01T00:00:00Z",
            "actual_outcome": "deny", "payload": {"case_id": case_id, "submitted_by": "Example",
                "grade": 2, "category": "travel", "amount_gbp": 100, "receipt": "yes",
                "days_notice": 30, "director_approval": "yes", "alcohol": "no",
                "client_driven": "no", "note": "Imported example"}, **changes}


def verdict(outcome="approve"):
    return Verdict(outcome=outcome, rationale="test", confidence=.9, policy_clause="1.1")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    include = tmp_path / "include"
    for folder in ("domains", "policies"):
        shutil.copytree(config.INCLUDE_DIR / folder, include / folder)
    monkeypatch.setattr(config, "INCLUDE_DIR", include)
    config.load_domain.cache_clear()
    yield include
    config.load_domain.cache_clear()


def test_shared_run_id_preserves_domains_and_judge_pass(fresh_db):
    for domain in ("expenses", "refunds"):
        store.save_replay("scheduled__same", domain, "v2", "actual", 1, [], 0, {domain: verdict()})
        store.save_verdicts("scheduled__same", domain, "v2", {domain: verdict()})
    assert len(store.query("SELECT * FROM runs")) == 2
    assert len(store.query("SELECT * FROM verdicts")) == 4
    for domain in ("expenses", "refunds"):
        assert domain in store.latest_verdicts(domain, "v2")


def test_no_change_supersedes_flip_in_every_read_model(fresh_db):
    ingest.import_cases("expenses", [row()], write=True)
    domain = config.load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime.max)
    for run_id, outcome in (("first", "approve"), ("later", "deny")):
        vs = {cases[0].case_id: verdict(outcome)}
        flips = diff.flips(cases, vs, domain)
        store.save_replay(run_id, "expenses", "v2", "actual", 1, flips, 0, vs,
                          case_segments=diff.case_segment_rows(cases, domain))
    assert not store.flips_for_policy("expenses", "v2")
    assert not report.flips("expenses", "v2")
    assert report.summary("expenses", "v2")["flips"] == 0
    assert not store.clause_breakdown("expenses", "v2")
    assert all(r["flips"] == 0 for r in store.segment_breakdown("expenses", "v2"))
    assert store.version_totals("expenses")["v2"]["flips"] == 0
    store.init_db()
    store.prune(days=0)
    assert not report.flips("expenses", "v2")


@pytest.mark.parametrize("changed", ["rules", "instructions", "model", "endpoint", "payload", "system"])
def test_cache_invalidates_judgment_inputs(changed, monkeypatch):
    domain = config.load_domain("expenses").model_copy(deep=True)
    case = Case(case_id="x", domain="expenses", decided_at=datetime(2025, 1, 1),
                payload=row()["payload"], actual_outcome="deny")
    judge = {"model": "offline", "endpoint": "one"}
    before = cache.judgment_key(case, domain, "v2", judge)
    if changed == "rules":
        domain.offline_rules["v2"] = [{"when": "True", "outcome": "deny"}]
    elif changed == "instructions":
        domain.judge_instructions += " Be precise."
    elif changed == "model":
        judge["model"] = "another"
    elif changed == "endpoint":
        judge["endpoint"] = "two"
    elif changed == "system":
        monkeypatch.setattr(provenance, "SYSTEM_PROMPT", "different instructions")
    else:
        case.payload["hidden_field"] = 42
    assert before != cache.judgment_key(case, domain, "v2", judge)


@pytest.mark.parametrize("name", ["../refunds/v1", "..\\refunds\\v1", "/tmp/v1", "C:\\v1", "x: y", ".hidden"])
def test_adoption_rejects_unsafe_names(workspace, fresh_db, name):
    folder = workspace / "drafts" / "expenses"
    folder.mkdir(parents=True)
    (folder / "v2-draft1.md").write_text("# Draft\n", encoding="utf-8")
    before = (workspace / "policies/refunds/v1.md").read_bytes()
    with pytest.raises(ValueError):
        proposal.adopt("expenses", "v2-draft1", "Reviewer", as_version=name)
    assert (workspace / "policies/refunds/v1.md").read_bytes() == before
    assert (folder / "v2-draft1.md").exists()


def test_adoption_refuses_unregistered_existing_file(workspace, fresh_db):
    folder = workspace / "drafts/expenses"
    folder.mkdir(parents=True)
    (folder / "v2-draft1.md").write_text("# Draft", encoding="utf-8")
    destination = workspace / "policies/expenses/v3.md"
    destination.write_text("Preserve me", encoding="utf-8")
    with pytest.raises(ValueError):
        proposal.adopt("expenses", "v2-draft1", "Reviewer", as_version="v3", dry_run=True)
    assert destination.read_text() == "Preserve me"


def test_import_preview_mapping_utc_and_duplicates(fresh_db):
    incoming = row(decided_at="2025-01-01T10:00:00+10:00")
    incoming["external_id"] = incoming.pop("case_id")
    assert ingest.import_cases("expenses", [incoming], mapping={"case_id": "external_id"})["accepted"] == 1
    assert not store.query("SELECT * FROM cases")
    result = ingest.import_cases("expenses", [incoming], mapping={"case_id": "external_id"}, write=True)
    assert result["written"] == 1
    assert store.query("SELECT decided_at FROM cases")[0]["decided_at"] == "2025-01-01T00:00:00"
    assert ingest.import_cases("expenses", [row()])["rejected"]
    assert ingest.import_cases("expenses", [row()], duplicates="skip", write=True)["skipped"]


@pytest.mark.parametrize("bad", [row(actual_outcome="maybe"), row(decided_at="bad"),
                                row(payload={}), row(subject_id=""), row(domain="refunds"),
                                row(payload="[]"), 42])
def test_import_rejects_entire_batch(fresh_db, bad):
    result = ingest.import_cases("expenses", [row("valid"), bad], write=True)
    assert result["rejected"] and not result["committed"]
    assert not store.query("SELECT * FROM cases")


def test_import_point_in_time_facts(fresh_db):
    incoming = row()
    del incoming["payload"]["grade"]
    facts = [{"subject_id": "import-subject", "key": "grade", "value": 2, "known_from": "2024-01-01Z".replace("Z", "")},
             {"subject_id": "import-subject", "key": "grade", "value": 5, "known_from": "2026-01-01"}]
    assert ingest.import_cases("expenses", [incoming], facts, write=True)["written"] == 1
    assert store.load_cases("expenses", until=datetime.max)[0].payload["grade"] == "2"
    facts[0]["value"] = 9
    assert ingest.import_cases("expenses", [], facts, write=True)["rejected"]


def test_csv_and_json_cli(tmp_path, fresh_db, capsys):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([row()]), encoding="utf-8")
    assert ingest.main(["expenses", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["preview"]
    assert ingest.main(["expenses", str(path), "--write"]) == 0
    csv_path = tmp_path / "cases.csv"
    csv_path.write_text("case_id,subject_id,decided_at,actual_outcome\nx,s,bad,deny\n", encoding="utf-8")
    assert ingest.main(["expenses", str(csv_path)]) == 1


def test_import_replay_snapshots_review_and_coverage(fresh_db, workspace, tmp_path):
    assert ingest.import_cases("expenses", [row(), row("import-2")], write=True)["written"] == 2
    result = replay.replay("expenses", "v2", limit=1)
    assert result["selected"] == 1 and result["capped"]
    coverage = report.coverage("expenses", "v2")
    assert coverage["missing"] == 1 and not coverage["complete"]
    replay.replay("expenses", "v2")
    assert report.coverage("expenses", "v2")["complete"]
    review = report.review_case("expenses", "v2", "import-1")
    assert review["archived_inputs"]
    assert review["baseline"]["version"] == "v1"
    assert review["candidate"]["policy_text"]
    path = tmp_path / "evidence.json"
    assert provenance.main(["expenses", "v2", "--snapshots", "-o", str(path)]) == 0
    assert json.loads(path.read_text())[0]["inputs"]
    original = review["candidate"]["policy_text"]
    (workspace / "policies/expenses/v2.md").write_text("# Changed policy", encoding="utf-8")
    assert report.coverage("expenses", "v2")["runs"][0]["stale"]
    assert report.review_case("expenses", "v2", "import-1")["candidate"]["policy_text"] == original


def test_pending_and_changed_inputs_are_not_complete(fresh_db):
    ingest.import_cases("expenses", [row()], write=True)
    domain = config.load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime.max)
    provenance.begin("pending", "expenses", "v2", provenance.capture(domain, "v2", cases))
    assert report.coverage("expenses", "v2")["runs"][0]["status"] == "pending"
    assert not report.coverage("expenses", "v2")["complete"]
    with store.conn() as c:
        c.execute("UPDATE cases SET actual_outcome='approve'")
    assert provenance.snapshots("expenses", "v2")[0]["stale"]


def test_server_search_finds_case_beyond_first_page(fresh_db):
    ingest.import_cases("expenses", [row(f"case-{n:03}") for n in range(205)], write=True)
    domain = config.load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime.max)
    vs = {c.case_id: verdict() for c in cases}
    flips = diff.flips(cases, vs, domain)
    store.save_replay("many", "expenses", "v2", "actual", len(cases), flips, 0, vs)
    assert len(report.flip_page("expenses", "v2")["items"]) == 200
    assert len(report.flip_page("expenses", "v2", offset=200)["items"]) == 5
    result = report.flip_page("expenses", "v2", search="case-204")
    assert result["matched"] == 1 and result["total"] == 205
    assert result["items"][0]["case_id"] == "case-204"
    assert report.flip_page("expenses", "v2", search="%' OR 1=1")["matched"] == 0
