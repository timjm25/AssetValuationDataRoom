"""Flask API (default port 5104).

GET  /healthz                  liveness
GET  /openapi.json             minimal served contract (agent.yaml invocation.openapi)
POST /value                    body {"lineage": {...}, "overrides": {...}} (or a bare lineage) -> valuation
POST /sensitivity              same body -> tornado + bear/base/bull scenarios
GET  /assets/<asset_id>/value  lineage fetched from AssetProgramRegistry (ASSET_REGISTRY_URL); 502 if down
POST /dataroom/gaps            body {"lineage": {...}, "documents": ["file names", ...]} -> stage-aware checklist
"""
from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, jsonify, request

from . import __version__
from .dataroom import gaps_for_names
from .engine import scenarios, sensitivity, value
from .inputs import InputError
from .registry_client import AssetNotFound, RegistryUnavailable, fetch_lineage, registry_url

OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "AssetValuationDataRoom", "version": __version__},
    "paths": {
        "/healthz": {"get": {"summary": "Liveness"}},
        "/value": {"post": {"summary": "rNPV from a lineage record plus overrides"}},
        "/sensitivity": {"post": {"summary": "Tornado and bear/base/bull scenarios"}},
        "/assets/{asset_id}/value": {"get": {"summary": "rNPV for a registry asset (fetches lineage)"}},
        "/dataroom/gaps": {"post": {"summary": "Stage-aware data room checklist and gaps"}},
    },
}


def _body() -> tuple[dict[str, Any], dict[str, Any]]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InputError("Request body must be a JSON object")
    lineage = data.get("lineage", data if "asset_id" in data else None)
    if not isinstance(lineage, dict):
        raise InputError("Body needs 'lineage' (an AssetProgramRegistry lineage record)")
    overrides = data.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise InputError("'overrides' must be an object")
    return lineage, overrides


def _query_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in request.args.getlist("override"):
        if "=" not in item:
            raise InputError(f"override {item!r} must look like key=value")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def create_app(registry_base_url: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.config["REGISTRY_URL"] = registry_base_url

    @app.errorhandler(InputError)
    def _input_error(exc: InputError):  # type: ignore[no-untyped-def]
        return jsonify({"error": str(exc)}), 400

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        return jsonify({"status": "ok", "service": "asset-valuation", "version": __version__})

    @app.get("/openapi.json")
    def openapi():  # type: ignore[no-untyped-def]
        return jsonify(OPENAPI)

    @app.post("/value")
    def post_value():  # type: ignore[no-untyped-def]
        lineage, overrides = _body()
        out = value(lineage, overrides).to_dict()
        if request.args.get("sensitivity") in ("1", "true", "yes"):
            out["sensitivity"] = sensitivity(lineage, overrides)
            out["scenarios"] = scenarios(lineage, overrides)
        return jsonify(out)

    @app.post("/sensitivity")
    def post_sensitivity():  # type: ignore[no-untyped-def]
        lineage, overrides = _body()
        return jsonify({"sensitivity": sensitivity(lineage, overrides), "scenarios": scenarios(lineage, overrides)})

    @app.get("/assets/<asset_id>/value")
    def asset_value(asset_id: str):  # type: ignore[no-untyped-def]
        try:
            lineage = fetch_lineage(asset_id, app.config["REGISTRY_URL"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except AssetNotFound as exc:
            return jsonify({"error": str(exc)}), 404
        except RegistryUnavailable as exc:
            return jsonify({"error": str(exc), "registry_url": registry_url(app.config["REGISTRY_URL"]),
                            "hint": "Start AssetProgramRegistry or set ASSET_REGISTRY_URL; or POST the "
                                    "lineage to /value directly."}), 502
        out = value(lineage, _query_overrides()).to_dict()
        out["lineage_source"] = f"{registry_url(app.config['REGISTRY_URL'])}/assets/{asset_id}/lineage"
        return jsonify(out)

    @app.post("/dataroom/gaps")
    def dataroom_gaps():  # type: ignore[no-untyped-def]
        lineage, _ = _body()
        data = request.get_json(silent=True) or {}
        names = data.get("documents") or []
        if not isinstance(names, list):
            raise InputError("'documents' must be a list of file names")
        return jsonify(gaps_for_names(lineage, [str(n) for n in names]))

    return app


def main() -> None:
    """Run the development server (PORT env, default 5104)."""
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5104")))


if __name__ == "__main__":
    main()
