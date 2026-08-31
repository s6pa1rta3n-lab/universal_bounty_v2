with open("src/engines/executor_engine.py", "r") as f:
    code = f.read()
code = code.replace("def claim_and_execute_lead(\n", "def claim_and_execute_lead(\n")
code = code.replace("strategy: str | None = None,\n    ) -> dict[str, Any]:", "strategy: str | None = None,\n    ) -> dict[str, Any]:\n        if 'base-org' in str(lead_id) or (lead_data and 'base-org' in str(lead_data)):\n            return {'success': False, 'error': 'Blocked by hotfix'}")
with open("src/engines/executor_engine.py", "w") as f:
    f.write(code)
