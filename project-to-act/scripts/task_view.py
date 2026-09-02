#!/usr/bin/env python3
"""Read a Project-to-Act task bundle and preview role handoffs without writing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 1024 * 1024
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CANONICAL_STATES = {"draft", "ready", "in_progress", "blocked", "review", "done", "cancelled"}

ROLE_CONTRACTS: dict[str, dict[str, Any]] = {
    "lead_to_builder.build_contract": {
        "producer": "lead",
        "consumer": "builder",
        "states": {"ready"},
        "schema": "project-to-act/build-contract@1",
        "fields": {
            "objective": str,
            "valueOrReason": str,
            "inScope": list,
            "outOfScope": list,
            "acceptanceScenarios": list,
            "architectureConstraints": list,
            "interfacesAndDependencies": list,
            "expectedDeliverables": list,
            "requiredSelfChecks": list,
            "autonomyBoundary": str,
            "escalationTriggers": list,
        },
    },
    "lead_to_verifier.verification_charter": {
        "producer": "lead",
        "consumer": "verifier",
        "states": {"ready", "in_progress", "review"},
        "schema": "project-to-act/verification-charter@1",
        "fields": {
            "claimsToVerify": list,
            "acceptanceScenarios": list,
            "riskAreas": list,
            "forbiddenRegressions": list,
            "boundaryConditions": list,
            "requiredIndependence": str,
            "releaseBlockingConditions": list,
            "allowedTuningScope": list,
        },
    },
    "builder_to_verifier.verification_candidate": {
        "producer": "builder",
        "consumer": "verifier",
        "states": {"review"},
        "schema": "project-to-act/verification-candidate@1",
        "fields": {
            "implementationSummary": str,
            "changedComponents": list,
            "changeRefs": list,
            "designDecisions": list,
            "deviationsFromContract": list,
            "selfTestEvidenceRefs": list,
            "setupOrMigrationSteps": list,
            "knownLimitations": list,
            "suspectedRiskAreas": list,
            "requestedTestFocus": list,
        },
    },
    "verifier_to_builder.change_request": {
        "producer": "verifier",
        "consumer": "builder",
        "states": {"review"},
        "schema": "project-to-act/change-request@1",
        "fields": {"overallVerdict": str, "findings": list},
    },
    "builder_to_lead.architecture_escalation": {
        "producer": "builder",
        "consumer": "lead",
        "states": {"in_progress", "blocked"},
        "schema": "project-to-act/architecture-escalation@1",
        "fields": {
            "discoveredConstraint": str,
            "impactedDecisions": list,
            "affectedScope": list,
            "optionsConsidered": list,
            "recommendedOption": str,
            "costAndRisk": str,
            "decisionNeeded": str,
            "workSafeToContinue": list,
        },
    },
    "verifier_to_lead.gate_recommendation": {
        "producer": "verifier",
        "consumer": "lead",
        "states": {"review"},
        "schema": "project-to-act/gate-recommendation@1",
        "fields": {
            "verdict": str,
            "coverageSummary": str,
            "acceptanceMatrix": list,
            "blockingFindings": list,
            "residualRisks": list,
            "regressionEvidenceRefs": list,
            "qualityOrPerformanceDelta": list,
            "unverifiedAreas": list,
            "exceptionsRequested": list,
            "recommendation": str,
            "confidence": str,
        },
    },
}


class ContractError(ValueError):
    def __init__(self, code: str, message: str, recovery: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.recovery = recovery
        self.exit_code = exit_code


def _project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ContractError("PROVIDER_NOT_FOUND", f"Project root is not a directory: {root}", "Pass an existing project root.", 3)
    return root


def _task_directory(root: Path, task_id: str) -> Path:
    if not TASK_ID.fullmatch(task_id):
        raise ContractError("INVALID_TASK_ID", f"Invalid task id: {task_id!r}", "Use letters, digits, dot, underscore, or hyphen.")
    directory = root / ".project-to-act" / "tasks" / task_id
    if directory.is_symlink() or not directory.is_dir():
        raise ContractError("PROVIDER_NOT_FOUND", f"Target task bundle not found: {directory}", "Pass an existing target task id.", 3)
    return directory


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError("TARGET_BUNDLE_INVALID", f"Missing regular file: {label}", "Restore the required task bundle file.")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ContractError("TARGET_BUNDLE_INVALID", f"JSON file exceeds {MAX_JSON_BYTES} bytes: {label}", "Reduce the source file to the supported contract size.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("TARGET_BUNDLE_INVALID", f"Invalid UTF-8 JSON in {label}", "Repair the source JSON before retrying.") from error
    if not isinstance(value, dict):
        raise ContractError("TARGET_BUNDLE_INVALID", f"{label} must contain a JSON object", "Repair the source JSON before retrying.")
    return value


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path, "role payload")
    except ContractError as error:
        raise ContractError("ROLE_PAYLOAD_INVALID", str(error), "Provide a regular UTF-8 JSON payload object.") from error


def _identity(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip() == "unassigned":
        return None
    return value.strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _gap(gaps: list[dict[str, str]], code: str, field: str, detail: str) -> None:
    gaps.append({"code": code, "field": field, "detail": detail})


def _normalize_items(value: Any, kind: str, gaps: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _gap(gaps, "TARGET_MAPPING_GAP", kind, f"TASK.{kind} is not an array")
        return []
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            if kind == "acceptance":
                normalized.append({"text": item.strip(), "status": "unknown"})
            else:
                normalized.append({"text": item.strip(), "status": "unknown", "independence": "unknown"})
            _gap(gaps, "TARGET_MAPPING_GAP", f"{kind}[{index}].status", "String items do not carry item-level status")
            continue
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ContractError("TARGET_BUNDLE_INVALID", f"TASK.{kind}[{index}] must be a non-empty string or object with text", "Repair the task contract.")
        if kind == "acceptance":
            status = item.get("status", "unknown")
            if status not in {"pending", "satisfied", "failed", "unknown"}:
                raise ContractError("TARGET_BUNDLE_INVALID", f"Invalid acceptance status at index {index}", "Use pending, satisfied, failed, or unknown.")
            normalized.append({"text": item["text"].strip(), "status": status})
        else:
            status = item.get("status", "unknown")
            independence = item.get("independence", "unknown")
            if status not in {"pending", "passed", "failed", "unknown"}:
                raise ContractError("TARGET_BUNDLE_INVALID", f"Invalid verification status at index {index}", "Use pending, passed, failed, or unknown.")
            if independence not in {"self", "independent", "unknown"}:
                raise ContractError("TARGET_BUNDLE_INVALID", f"Invalid verification independence at index {index}", "Use self, independent, or unknown.")
            normalized.append({"text": item["text"].strip(), "status": status, "independence": independence})
    return normalized


def canonical_view(project_root: Path, task_id: str) -> dict[str, Any]:
    root = project_root.resolve()
    directory = _task_directory(root, task_id)
    task = _read_json(directory / "TASK.json", f"{task_id}/TASK.json")
    status = _read_json(directory / "STATUS.json", f"{task_id}/STATUS.json")
    if task.get("schemaVersion") != SCHEMA_VERSION or status.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("CONTRACT_VERSION_UNSUPPORTED", "TASK.json and STATUS.json must use schemaVersion 1", "Migrate the task bundle or use a compatible provider.", 2)
    if task.get("taskId") != task_id or status.get("taskId") != task_id:
        raise ContractError("TARGET_BUNDLE_INVALID", "TASK.json and STATUS.json taskId values must match the requested task", "Repair the task bundle ids.")

    title = task.get("title")
    goal = task.get("goal")
    if not isinstance(title, str) or not title.strip() or not isinstance(goal, str) or not goal.strip():
        raise ContractError("TARGET_BUNDLE_INVALID", "TASK.title and TASK.goal must be non-empty strings", "Complete the target task contract.")

    state = status.get("state")
    if state not in CANONICAL_STATES:
        raise ContractError("TARGET_BUNDLE_INVALID", f"Unknown canonical state: {state!r}", "Use a canonical task state.")
    revision = status.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ContractError("TARGET_BUNDLE_INVALID", "STATUS.revision must be a non-negative integer", "Repair STATUS.json.")

    gaps: list[dict[str, str]] = []
    scope = task.get("scope") if isinstance(task.get("scope"), dict) else {}
    allowed = _string_list(scope.get("allowed"))
    non_goals = _string_list(scope.get("nonGoals"))
    if not allowed:
        _gap(gaps, "TARGET_MAPPING_GAP", "scope.allowed", "Target task does not declare allowed scope")
    if not non_goals:
        _gap(gaps, "TARGET_MAPPING_GAP", "scope.nonGoals", "Target task does not declare non-goals")

    handoff: dict[str, Any] = {}
    handoff_path = directory / "HANDOFF.json"
    if handoff_path.exists():
        handoff = _read_json(handoff_path, f"{task_id}/HANDOFF.json")
    handoff_state = status.get("handoffState", handoff.get("state"))
    if handoff_state not in {None, "published", "accepted"}:
        raise ContractError("TARGET_BUNDLE_INVALID", f"Unknown handoff state: {handoff_state!r}", "Repair STATUS.json or HANDOFF.json.")

    base_revision = status.get("baseRevision")
    if base_revision is not None and (not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0):
        raise ContractError("TARGET_BUNDLE_INVALID", "STATUS.baseRevision must be a non-negative integer or null", "Repair STATUS.json.")
    if base_revision is None:
        _gap(gaps, "TARGET_MAPPING_GAP", "baseRevision", "Target task does not declare a base revision")

    evidence_directory = directory / "evidence"
    evidence_refs = []
    if evidence_directory.is_dir() and not evidence_directory.is_symlink():
        evidence_refs = [path.relative_to(root).as_posix() for path in sorted(evidence_directory.glob("*.json")) if path.is_file() and not path.is_symlink()]

    acceptance = _normalize_items(task.get("acceptance"), "acceptance", gaps)
    verification = _normalize_items(task.get("verification"), "verification", gaps)
    next_action = status.get("nextAction", handoff.get("nextAction"))
    if not isinstance(next_action, str) or not next_action.strip():
        next_action = None
        _gap(gaps, "TARGET_MAPPING_GAP", "nextAction", "Target task does not declare an exact next action")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "provider": "project-to-act",
        "source": {"kind": "target-task-bundle", "path": directory.relative_to(root).as_posix()},
        "taskId": task_id,
        "title": title.strip(),
        "goal": goal.strip(),
        "scope": {"allowed": allowed, "nonGoals": non_goals},
        "owner": _identity(status.get("owner") or task.get("owner")),
        "nextOwner": _identity(status.get("nextOwner") or handoff.get("to")),
        "state": state,
        "handoffState": handoff_state,
        "revision": revision,
        "baseRevision": base_revision,
        "acceptance": acceptance,
        "verification": verification,
        "evidenceRefs": evidence_refs,
        "nextAction": next_action,
        "conflicts": status.get("conflicts") if isinstance(status.get("conflicts"), list) else [],
        "gaps": gaps,
    }


def role_preview(view: dict[str, Any], handoff_type: str, payload: Any) -> dict[str, Any]:
    contract = ROLE_CONTRACTS.get(handoff_type)
    if contract is None:
        raise ContractError("ROLE_DIRECTION_CONFLICT", f"Unknown handoff type: {handoff_type}", "Choose one of the six supported role directions.")
    if view["state"] not in contract["states"]:
        allowed = ", ".join(sorted(contract["states"]))
        raise ContractError("ROLE_STATE_CONFLICT", f"{handoff_type} is not legal from task state {view['state']!r}; allowed: {allowed}", "Move the task through an explicit core transition or choose the correct handoff type.")
    if not isinstance(payload, dict):
        raise ContractError("ROLE_PAYLOAD_INVALID", "Role payload must be a JSON object", "Provide the versioned payload object.")
    errors = []
    for field, expected_type in contract["fields"].items():
        value = payload.get(field)
        if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
            errors.append(f"{field} must be {expected_type.__name__}")
    if errors:
        raise ContractError("ROLE_PAYLOAD_INVALID", "; ".join(errors), "Complete the required role payload fields without inventing evidence.")
    if handoff_type == "verifier_to_builder.change_request":
        finding_fields = {
            "findingId": str,
            "severity": str,
            "category": str,
            "evidenceRef": str,
            "reproductionSteps": list,
            "expected": str,
            "actual": str,
            "affectedScope": list,
            "confidence": str,
            "requiredChange": str,
            "retestCriteria": list,
        }
        if payload["overallVerdict"] != "changes-required":
            raise ContractError("ROLE_PAYLOAD_INVALID", "overallVerdict must be 'changes-required'", "Use the fixed change-request verdict.")
        for index, finding in enumerate(payload["findings"]):
            if not isinstance(finding, dict):
                raise ContractError("ROLE_PAYLOAD_INVALID", f"findings[{index}] must be an object", "Provide a structured verifier finding.")
            for field, expected_type in finding_fields.items():
                value = finding.get(field)
                if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
                    raise ContractError("ROLE_PAYLOAD_INVALID", f"findings[{index}].{field} must be {expected_type.__name__}", "Complete the structured verifier finding.")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "valid": True,
        "writes": False,
        "authorizationValidated": False,
        "taskId": view["taskId"],
        "taskState": view["state"],
        "producerRole": contract["producer"],
        "consumerRole": contract["consumer"],
        "handoffType": handoff_type,
        "payloadSchema": contract["schema"],
        "payload": payload,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    view = subparsers.add_parser("view", help="Emit canonical-task-view@1")
    preview = subparsers.add_parser("role-preview", help="Validate and preview a role handoff without writing")
    for command in (view, preview):
        command.add_argument("--project-root", required=True)
        command.add_argument("--task-id", required=True)
    preview.add_argument("--handoff-type", required=True)
    preview.add_argument("--payload-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _project_root(args.project_root)
        view = canonical_view(root, args.task_id)
        output: dict[str, Any] = view
        if args.command == "role-preview":
            payload = _read_payload(Path(args.payload_file).expanduser().resolve())
            output = role_preview(view, args.handoff_type, payload)
        sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
        return 0
    except ContractError as error:
        sys.stderr.write(json.dumps({"code": error.code, "message": str(error), "recovery": error.recovery}, ensure_ascii=False) + "\n")
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
