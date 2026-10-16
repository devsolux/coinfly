import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from coinfly import __application_id__, __project_name__, __version__
from coinfly.config.models import EngineConfig, default_config
from coinfly.config.service import decode_operator_config
from coinfly.domain.context import DomainError, Environment, ProductTrack, RegionPolicy, RunProfile


class ContextContractTests(unittest.TestCase):
    def test_product_metadata(self):
        self.assertEqual((__project_name__, __version__, __application_id__),
                         ("coinfly", "1.0.0", "com.devsolux.coinfly"))

    def test_environment_cannot_configure_trading(self):
        with patch.dict(os.environ, {"COINFLY_MODE": "LIVE", "COINBASE_API_KEY": "canary",
                                     "OPENAI_API_KEY": "canary", "COINFLY_DATA": "/evil"}):
            self.assertEqual(default_config().environment, Environment.SANDBOX)
            self.assertIsNone(default_config().credential_binding_id)

    def test_germany_perpetual_is_resolved_not_unconditionally_rejected(self):
        from datetime import datetime, timezone

        from coinfly.domain.products import ProductSubtype
        from coinfly.venues.capability import CapabilityRequest, Category, resolve

        config = EngineConfig(authority_id="a", product_track=ProductTrack.PERPETUAL,
                              region_policy=RegionPolicy.GERMANY, execution_scope=("p",))
        # The EEA contract model is the default meaning of PERPETUAL under GERMANY.
        self.assertEqual(config.effective_subtype, ProductSubtype.PERPETUAL_STYLE_FUTURE)
        with self.assertRaises(ValidationError):
            EngineConfig(authority_id="a", product_track=ProductTrack.SPOT,
                         product_subtype=ProductSubtype.PERPETUAL_NO_EXPIRY, execution_scope=("p",))
        request = CapabilityRequest(environment=Environment.SANDBOX, region_policy=RegionPolicy.GERMANY,
                                    product_track=ProductTrack.PERPETUAL, product_subtype=ProductSubtype.PERPETUAL_NO_EXPIRY)
        decision = resolve(request, evidence=None, now=datetime.now(timezone.utc))
        self.assertEqual(decision.category, Category.POLICY_REJECTED)
        self.assertEqual(decision.codes, ("GERMANY_POLICY_EXCLUDES_INTERNATIONAL_PERPETUALS",))

    def test_shadow_is_synthetic_and_has_no_live_credential(self):
        config = EngineConfig(authority_id="a", execution_scope=("p",), run_profile=RunProfile.SHADOW)
        self.assertEqual(config.endpoint_profile_id, "paper-local")
        data = config.model_dump(mode="json")
        data["credential_binding_id"] = "live-secret"
        with self.assertRaises(DomainError):
            decode_operator_config(data)

    def test_operator_cannot_supply_attestation_or_secret_and_errors_redacted(self):
        for field in ("private_key", "verified_profile", "legal_entity", "api_key"):
            data = default_config().model_dump(mode="json")
            data[field] = "SECRET_CANARY"
            with self.assertRaises(DomainError) as caught:
                decode_operator_config(data)
            self.assertNotIn("SECRET_CANARY", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_numeric_strings_are_decimal_at_json_boundary_but_float_never_enters_config(self):
        data = default_config().model_dump(mode="json")
        config = decode_operator_config(data)
        self.assertEqual(config.digest, default_config().digest)
        with self.assertRaises(ValidationError):
            EngineConfig(authority_id="a", execution_scope=["p"])

    def test_immutable_configuration(self):
        config = default_config()
        with self.assertRaises(ValidationError):
            config.environment = Environment.LIVE
        self.assertEqual(config, EngineConfig.model_validate_json(config.model_dump_json()))

    def test_active_imports_do_not_include_ambient_sdk_or_dotenv(self):
        root = Path(__file__).resolve().parents[2] / "src" / "coinfly"
        bad = {"dotenv", "pydantic_settings", "coinbase_agentkit", "coinbase.rest"}
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for module in modules:
                    self.assertFalse(any(module == x or module.startswith(x + ".") for x in bad),
                                     f"ambient/authority-bearing import in {path}: {module}")

    def test_ui_cannot_import_worker_database_or_broker(self):
        root = Path(__file__).resolve().parents[2] / "src" / "coinfly" / "ui"
        forbidden = ("sqlite3", "coinfly.storage", "coinfly.execution", "coinfly.security", "coinfly.venues")
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse(any(m == f or m.startswith(f + ".") for m in modules for f in forbidden),
                                 str(path))

    def test_no_nan_json_config(self):
        data = default_config().model_dump(mode="json")
        data["limits"]["max_order_notional"] = float("nan")
        with self.assertRaises(DomainError):
            decode_operator_config(data)


if __name__ == "__main__":
    unittest.main()
