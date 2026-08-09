"""Unit tests for CloudWatch metric helper (mocked)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cardtrader_inventory.aws.metrics import put_metrics


class MetricsTests(unittest.TestCase):
    @patch("cardtrader_inventory.aws.metrics._cloudwatch_client")
    def test_put_metrics(self, mock_client_factory: MagicMock) -> None:
        client = MagicMock()
        mock_client_factory.return_value = client
        put_metrics(
            {
                "CardsInInventory": (10, "Count"),
                "InventoryValue": (1234.56, "None"),
            },
            namespace="CardTraderInventory/Reprice",
        )
        client.put_metric_data.assert_called_once()
        kwargs = client.put_metric_data.call_args.kwargs
        self.assertEqual(kwargs["Namespace"], "CardTraderInventory/Reprice")
        by_name = {m["MetricName"]: m for m in kwargs["MetricData"]}
        self.assertEqual(by_name["CardsInInventory"]["Unit"], "Count")
        self.assertEqual(by_name["InventoryValue"]["Value"], 1234.56)

    @patch("cardtrader_inventory.aws.metrics._cloudwatch_client")
    def test_empty_noop(self, mock_client_factory: MagicMock) -> None:
        put_metrics({})
        mock_client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
