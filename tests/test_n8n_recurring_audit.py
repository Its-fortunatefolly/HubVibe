"""Static contract tests for the public n8n recurring-audit template.

The tests deliberately make no network request and spend no money. They verify
the portable workflow contract, credential hygiene, bounded retry identity,
delivery validation, and the adoption marker consumed by existing HubVibe
records.
"""

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / "integrations" / "n8n" / "hubvibe-recurring-audit.json"
GUIDE_PATH = REPO_ROOT / "integrations" / "n8n" / "README.md"


def _workflow():
    with WORKFLOW_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _nodes_by_name(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def test_workflow_is_valid_inactive_json_with_unique_nodes():
    workflow = _workflow()
    names = [node["name"] for node in workflow["nodes"]]
    ids = [node["id"] for node in workflow["nodes"]]
    executable = [
        node for node in workflow["nodes"]
        if node["type"] != "n8n-nodes-base.stickyNote"
    ]
    notes = [
        node for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.stickyNote"
    ]

    assert workflow["name"] == "HubVibe recurring site audit"
    assert workflow["active"] is False
    assert workflow["settings"]["timezone"] == "UTC"
    assert len(names) == len(set(names)) == 8
    assert len(ids) == len(set(ids)) == 8
    assert len(executable) == 5
    assert len(notes) == 3


def test_manual_and_daily_triggers_converge_on_one_purchase_path():
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    assert nodes["Test workflow manually"]["type"] == "n8n-nodes-base.manualTrigger"
    schedule = nodes["Run daily at 09:00 UTC"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    assert schedule["parameters"]["rule"]["interval"] == [
        {
            "field": "days",
            "daysInterval": 1,
            "triggerAtHour": 9,
            "triggerAtMinute": 0,
        }
    ]

    for trigger in ("Test workflow manually", "Run daily at 09:00 UTC"):
        target = workflow["connections"][trigger]["main"][0]
        assert target == [{"node": "Set site URL", "type": "main", "index": 0}]

    assert sum(node["type"] == "n8n-nodes-base.httpRequest" for node in workflow["nodes"]) == 1


def test_target_is_an_obvious_safe_placeholder():
    target = _nodes_by_name(_workflow())["Set site URL"]
    assignments = target["parameters"]["assignments"]["assignments"]

    assert assignments == [
        {
            "id": "bc707e0c-b817-4e71-9808-c0b60e9a25dc",
            "name": "url",
            "value": "https://example.com",
            "type": "string",
        }
    ]


def test_http_node_calls_only_the_existing_bundle_endpoint():
    node = _nodes_by_name(_workflow())["Buy HubVibe audit bundle"]
    parameters = node["parameters"]

    assert parameters["method"] == "POST"
    assert parameters["url"] == "https://hubvibe-io.com/audit/bundle"
    assert parameters["sendBody"] is True
    assert parameters["rawContentType"] == "application/json"
    assert parameters["body"] == "={{ JSON.stringify({ url: $json.url }) }}"


def test_authentication_uses_an_unbound_header_credential_without_a_secret():
    workflow = _workflow()
    node = _nodes_by_name(workflow)["Buy HubVibe audit bundle"]

    assert node["parameters"]["authentication"] == "genericCredentialType"
    assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert "credentials" not in node

    serialized = json.dumps(workflow)
    lowered = serialized.lower()
    assert "hubvibe_api_key" not in lowered
    assert "wallet_key" not in lowered
    assert "private_key" not in lowered
    assert not re.search(r"\b(?:sk|hv)[-_][a-z0-9]{20,}\b", lowered)


def test_retry_is_bounded_and_reuses_a_per_execution_purchase_identity():
    node = _nodes_by_name(_workflow())["Buy HubVibe audit bundle"]
    headers = {
        item["name"]: item["value"]
        for item in node["parameters"]["headerParameters"]["parameters"]
    }

    assert node["retryOnFail"] is True
    assert node["maxTries"] == 2
    assert node["waitBetweenTries"] == 5000
    assert headers["Idempotency-Key"].startswith("={{ 'n8n-audit:'")
    assert "$workflow.id" in headers["Idempotency-Key"]
    assert "$execution.id" in headers["Idempotency-Key"]
    assert "$json.url" in headers["Idempotency-Key"]


def test_integration_markers_are_nonsecret_and_versioned():
    node = _nodes_by_name(_workflow())["Buy HubVibe audit bundle"]
    headers = {
        item["name"]: item["value"]
        for item in node["parameters"]["headerParameters"]["parameters"]
    }

    assert headers["User-Agent"] == "hubvibe-n8n-recurring-audit/1.0"
    assert headers["X-HubVibe-Integration"] == "n8n-recurring-audit/1.0"
    assert "X-API-Key" not in headers
    assert "Authorization" not in headers


def test_success_requires_the_delivered_audit_shape():
    workflow = _workflow()
    node = _nodes_by_name(workflow)["Require delivered result"]
    code = node["parameters"]["jsCode"]

    assert node["type"] == "n8n-nodes-base.code"
    assert "result.status !== 'ok'" in code
    assert "typeof result.pass !== 'boolean'" in code
    assert "throw new Error" in code
    assert workflow["connections"]["Buy HubVibe audit bundle"]["main"][0] == [
        {"node": "Require delivered result", "type": "main", "index": 0}
    ]


def test_template_notes_meet_current_n8n_submission_shape():
    workflow = _workflow()
    notes = {
        node["name"]: node
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.stickyNote"
    }

    overview = notes["Overview and setup"]
    content = overview["parameters"]["content"]
    word_count = len(re.findall(r"\b[\w$.-]+\b", content))
    assert overview["parameters"]["color"] == 1  # n8n yellow
    assert 100 <= word_count <= 300
    assert "### How it works" in content
    assert "### Setup" in content
    assert "### Customization" in content
    assert "X-API-Key" in content
    assert "$0.15" in content
    assert "missing or invalid credential cannot purchase" in content

    sections = [
        notes["Configure and trigger section"],
        notes["Purchase and validation section"],
    ]
    for section in sections:
        section_content = section["parameters"]["content"]
        assert section["parameters"]["color"] == 7  # n8n neutral/white
        assert section_content.startswith("## ")
        assert len(re.findall(r"\b[\w$.-]+\b", section_content)) < 50

    assert overview["position"][0] + overview["parameters"]["width"] <= sections[0]["position"][0]
    assert sections[0]["position"][0] + sections[0]["parameters"]["width"] <= sections[1]["position"][0]
    assert all(note["name"] not in workflow["connections"] for note in notes.values())


def test_guide_documents_credential_setup_activation_cost_and_unknown_outcomes():
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    for required in (
        "Header Auth",
        "X-API-Key",
        "inactive when imported",
        "$0.15 per successful delivery",
        "Publish the workflow only after the manual test succeeds",
        "unknown outcome",
        "new execution has a new purchase identity",
        "n8n-recurring-audit/1.0",
        "no analytics service",
        "Daily production storefront check",
        "Creator Dashboard",
    ):
        assert required in guide


def test_guide_does_not_claim_certification_or_guaranteed_financial_results():
    guide = GUIDE_PATH.read_text(encoding="utf-8").lower()

    assert "not a compliance certification" in guide
    assert "not revenue or profit forecasts" in guide
    assert "guaranteed" not in guide
