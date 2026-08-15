#!/usr/bin/env python3
"""Synchronize managed Open WebUI Workspace-model parameters.

Open WebUI treats DEFAULT_MODEL_PARAMS as inference defaults. In 0.10.x a
global ``system`` value is not reliably converted into an upstream system
message, while a Workspace model's params.system is.
"""

import argparse
import json
import sqlite3
import sys
import time


def sync(args: argparse.Namespace) -> None:
    with sqlite3.connect(args.database, timeout=30) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "model" not in tables or "user" not in tables:
            print("Open WebUI database is not initialized; model prompts will sync on the next start.")
            return

        owner = database.execute(
            """
            SELECT id
            FROM user
            ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at
            LIMIT 1
            """
        ).fetchone()
        if owner is None:
            print("Open WebUI has no user yet; model prompts will sync after the first account is created.")
            return

        owner_id = owner[0]
        now = int(time.time())
        for model_id, model_config in args.model_config.items():
            managed_params = model_config["params"]
            row = database.execute(
                "SELECT params, meta FROM model WHERE id = ?", (model_id,)
            ).fetchone()

            if row is None:
                params = {}
                if args.system_prompt:
                    params["system"] = args.system_prompt
                params.update(managed_params)
                meta = {"managed_by": "nixloom"}
                database.execute(
                    """
                    INSERT INTO model (
                        id, user_id, base_model_id, name, params, meta,
                        created_at, updated_at, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        model_id,
                        owner_id,
                        model_config["base_model_id"],
                        model_config["name"],
                        json.dumps(params, ensure_ascii=False),
                        json.dumps(meta),
                        now,
                        now,
                    ),
                )
                continue

            try:
                params = json.loads(row[0]) if row[0] else {}
            except (TypeError, json.JSONDecodeError):
                params = {}
            if args.system_prompt:
                params["system"] = args.system_prompt
            else:
                params.pop("system", None)
            params.update(managed_params)
            try:
                meta = json.loads(row[1]) if row[1] else {}
            except (TypeError, json.JSONDecodeError):
                meta = {}
            meta["managed_by"] = "nixloom"
            database.execute(
                """
                UPDATE model
                SET base_model_id = ?, name = ?, params = ?, meta = ?,
                    updated_at = ?, is_active = 1
                WHERE id = ?
                """,
                (
                    model_config["base_model_id"],
                    model_config["name"],
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(meta),
                    now,
                    model_id,
                ),
            )

        placeholders = ",".join("?" for _ in args.model_config)
        database.execute(
            f"""
            DELETE FROM model
            WHERE json_valid(meta)
              AND json_extract(meta, '$.managed_by') IN ('nixloom', 'llm-stack')
              AND id NOT IN ({placeholders})
            """,
            tuple(args.model_config),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--model-config-json", default="{}")
    args = parser.parse_args()

    try:
        args.model_config = json.loads(args.model_config_json)
    except json.JSONDecodeError as error:
        parser.error(f"--model-config-json is invalid JSON: {error}")
    if not isinstance(args.model_config, dict):
        parser.error("--model-config-json must contain an object")
    if not args.model_config:
        parser.error("--model-config-json must contain at least one model")
    for model_id, model_config in args.model_config.items():
        if not isinstance(model_config, dict):
            parser.error(f"model config for {model_id} must be an object")
        unknown = set(model_config) - {"name", "base_model_id", "params"}
        if unknown:
            parser.error(f"model config for {model_id} has unknown keys: {sorted(unknown)}")
        if not isinstance(model_config.get("name"), str) or not model_config["name"]:
            parser.error(f"model config for {model_id} needs a non-empty name")
        if model_config.get("base_model_id") is not None and not isinstance(
            model_config["base_model_id"], str
        ):
            parser.error(f"base_model_id for {model_id} must be a string or null")
        params = model_config.get("params")
        if not isinstance(params, dict):
            parser.error(f"params for {model_id} must be an object")
        threshold = params.get("compact_token_threshold")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            parser.error(
                f"compact_token_threshold for {model_id} must be a positive integer"
            )

    # Best-effort: this pokes Open WebUI's private schema, which a version
    # bump may change. A failed sync must never block the stack from starting.
    try:
        sync(args)
    except sqlite3.Error as error:
        print(
            f"warning: model prompt sync skipped ({error}); "
            "the Open WebUI schema may have changed.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
