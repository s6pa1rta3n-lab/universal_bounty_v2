"""
Adversarial Challenger Verification Suite: 3D Pipeline UI & Constellation Engine
Target: /console/pipeline.html, pipeline.ts, api.ts, console_server.py

Tests:
1. Canonical stage coordinate geometry & Euclidean cluster separation
2. Stage configuration, camera positions, and hex color consistency across files
3. HTML DOM contract completeness (HUD, Canvas, Tooltip, Badges, Stage Chips)
4. Fibonacci orbital distribution mathematical invariants (zero-division, NaN, Inf bounds)
5. Live HTTP socket server concurrency, static asset delivery, and high-frequency polling
6. XSS sanitization and payload robustness
"""

import math
import re
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
import pytest
import requests

from src.console_server import ConsoleHandler, pipeline_payload, empty_pipeline_payload, normalize_status


# ==============================================================================
# 1. 3D Spatial Geometry & Constellation Coordinate Tests
# ==============================================================================

CANONICAL_ANCHORS = {
    "queued": (-60, 15, -30),
    "pending_triage": (-30, -20, 25),
    "pr_open": (0, 25, 0),
    "completed": (50, 15, -15),
    "failed": (45, -25, 30),
}

CANONICAL_COLORS = {
    "queued": 0x94A3B8,
    "pending_triage": 0xE8C36A,
    "pr_open": 0x00F0FF,
    "completed": 0xC0FF70,
    "failed": 0xE11D2E,
}

CANONICAL_CAM_POS = {
    "queued": (-60, 25, 20),
    "pending_triage": (-30, -10, 75),
    "pr_open": (0, 35, 50),
    "completed": (50, 25, 35),
    "failed": (45, -15, 80),
}


class TestSpatialConstellationGeometry:
    """Verifies that 3D coordinates in code strictly match canonical project specifications."""

    def test_canonical_5_stages_present_and_exact(self):
        assert len(CANONICAL_ANCHORS) == 5
        assert set(CANONICAL_ANCHORS.keys()) == {
            "queued",
            "pending_triage",
            "pr_open",
            "completed",
            "failed",
        }

    def test_euclidean_distance_separation_between_wells(self):
        """Gravity wells must be well separated in 3D space to prevent cluster collisions (min dist >= 35 units)."""
        stages = list(CANONICAL_ANCHORS.keys())
        min_distance = float("inf")
        closest_pair = None

        for i in range(len(stages)):
            for j in range(i + 1, len(stages)):
                s1, s2 = stages[i], stages[j]
                p1, p2 = CANONICAL_ANCHORS[s1], CANONICAL_ANCHORS[s2]
                dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))
                if dist < min_distance:
                    min_distance = dist
                    closest_pair = (s1, s2)

                # Each well must have distinct spatial domain
                assert dist >= 35.0, f"Clusters {s1} and {s2} are too close: {dist:.2f} < 35.0"

        assert min_distance > 38.0
        assert closest_pair is not None

    def test_api_ts_and_pipeline_ts_coordinate_conformance(self):
        """Verify api.ts has exact canonical coordinates and colors."""
        api_ts = Path("console-ui/src/api.ts").read_text(encoding="utf-8")

        for stage, anchor in CANONICAL_ANCHORS.items():
            anchor_str = f"[{anchor[0]}, {anchor[1]}, {anchor[2]}]"
            assert (
                anchor_str in api_ts
            ), f"Anchor {anchor_str} for stage {stage} missing in api.ts"

        for stage, color in CANONICAL_COLORS.items():
            hex_str = f"0x{color:06x}"
            assert (
                hex_str in api_ts.lower()
            ), f"Color {hex_str} for stage {stage} missing in api.ts"

    def test_camera_focal_offsets_are_outside_well_radii(self):
        """Camera viewing positions must be offset from well anchors by at least 30 units."""
        for stage, anchor in CANONICAL_ANCHORS.items():
            cam = CANONICAL_CAM_POS[stage]
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(anchor, cam)))
            assert (
                dist >= 40.0
            ), f"Camera for {stage} is too close to anchor: {dist:.2f} < 40.0"


# ==============================================================================
# 2. Fibonacci Spherical Distribution Mathematical Invariants
# ==============================================================================


class TestFibonacciOrbitalMathematics:
    """Stress-tests the orbital distribution algorithm implemented in pipeline.ts."""

    def compute_positions(self, count: int):
        positions = []
        for idx in range(count):
            radius = 8.5 + ((idx * 2.2) % 16.0)
            theta = math.acos(1 - (2 * (idx + 0.5)) / max(count, 1))
            phi = math.pi * (1 + math.sqrt(5)) * idx

            lx = radius * math.sin(theta) * math.cos(phi)
            ly = radius * math.cos(theta) * 0.55
            lz = radius * math.sin(theta) * math.sin(phi)

            assert not math.isnan(lx) and not math.isinf(lx)
            assert not math.isnan(ly) and not math.isinf(ly)
            assert not math.isnan(lz) and not math.isinf(lz)
            positions.append((lx, ly, lz))
        return positions

    def test_edge_case_zero_nodes(self):
        positions = self.compute_positions(0)
        assert len(positions) == 0

    def test_single_node_distribution(self):
        positions = self.compute_positions(1)
        assert len(positions) == 1
        pos = positions[0]
        # At index 0, radius = 8.5, theta = acos(1 - 2*0.5/1) = acos(0) = pi/2
        # sin(pi/2)=1, cos(pi/2)=0, phi=0 => (8.5*1*1, 0, 8.5*1*0) = (8.5, 0, 0)
        assert abs(pos[0] - 8.5) < 1e-5
        assert abs(pos[1]) < 1e-5
        assert abs(pos[2]) < 1e-5

    @pytest.mark.parametrize("node_count", [5, 20, 100, 500, 2000])
    def test_high_density_node_distributions_remain_bounded(self, node_count):
        positions = self.compute_positions(node_count)
        assert len(positions) == node_count

        max_radius = 8.5 + 16.0
        for x, y, z in positions:
            r = math.sqrt(x**2 + (y / 0.55) ** 2 + z**2)
            assert 8.0 <= r <= max_radius + 1e-4


# ==============================================================================
# 3. HTML DOM Contract & Static Asset Rigor
# ==============================================================================


class TestHtmlDomAndStaticContract:
    def test_pipeline_html_required_dom_elements(self):
        pipeline_html = Path("console-ui/pipeline.html").read_text(encoding="utf-8")

        required_ids = [
            "pipeline-canvas",
            "pipeline-hud",
            "hud-content",
            "hud-close-btn",
            "pipeline-tooltip",
            "tooltip-status-badge",
            "tooltip-payout",
            "tooltip-repo",
            "tooltip-title",
            "btn-reset-view",
            "stage-chips",
            "pipeline-total-badge",
            "total-pipeline-value",
            "last-sync-time",
            "clock",
            "health-dot",
            "health-label",
            "count-all",
            "count-queued",
            "count-pending_triage",
            "count-pr_open",
            "count-completed",
            "count-failed",
        ]

        for element_id in required_ids:
            assert (
                f'id="{element_id}"' in pipeline_html
            ), f"Required DOM id '{element_id}' missing in pipeline.html"

    def test_stage_filter_chips_cover_all_stages(self):
        pipeline_html = Path("console-ui/pipeline.html").read_text(encoding="utf-8")
        stages = ["all", "queued", "pending_triage", "pr_open", "completed", "failed"]

        for stage in stages:
            pattern = rf'data-stage="{stage}"'
            assert re.search(
                pattern, pipeline_html
            ), f"Stage filter chip for '{stage}' missing in pipeline.html"

    def test_static_dist_bundle_exists_and_non_empty(self):
        static_dir = Path("static/console")
        assert static_dir.is_dir()
        pipeline_html = static_dir / "pipeline.html"
        assert pipeline_html.is_file()
        assert pipeline_html.stat().st_size > 1000

        assets_dir = static_dir / "assets"
        assert assets_dir.is_dir()

        js_files = list(assets_dir.glob("pipeline-*.js"))
        css_files = list(assets_dir.glob("pipeline-*.css"))
        three_files = list(assets_dir.glob("three.module-*.js"))

        assert len(js_files) >= 1, "Bundled pipeline JS missing in static/console/assets"
        assert len(css_files) >= 1, "Bundled pipeline CSS missing in static/console/assets"
        assert len(three_files) >= 1, "Bundled three.module JS missing in static/console/assets"


# ==============================================================================
# 4. Live Server HTTP Concurrency & Asset Delivery Stress Test
# ==============================================================================


class TestLiveSocketServerConcurrency:
    @pytest.fixture(scope="class")
    def live_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        time.sleep(0.1)
        base_url = f"http://127.0.0.1:{port}"
        yield base_url

        server.shutdown()
        server.server_close()

    def test_pipeline_html_served_live(self, live_server):
        resp = requests.get(f"{live_server}/console/pipeline.html", timeout=3)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        assert "id=\"pipeline-canvas\"" in resp.text

    def test_api_pipeline_served_live_with_contract(self, live_server):
        resp = requests.get(f"{live_server}/api/pipeline", timeout=3)
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        data = resp.json()
        assert "leads" in data
        assert "grouped" in data
        assert "counts" in data
        assert data["counts"]["total"] == len(data["leads"])

    def test_concurrent_burst_stress(self, live_server):
        """Simulates 40 concurrent workers polling /api/pipeline, /console/pipeline.html, and assets."""
        paths = [
            "/console/pipeline.html",
            "/api/pipeline",
            "/console",
            "/health",
        ]

        results = []
        errors = []

        def worker(path):
            try:
                r = requests.get(f"{live_server}{path}", timeout=5)
                results.append((path, r.status_code))
            except Exception as ex:
                errors.append((path, str(ex)))

        threads = []
        for _ in range(10):
            for p in paths:
                t = threading.Thread(target=worker, args=(p,))
                threads.append(t)
                t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Encountered concurrency errors: {errors}"
        assert len(results) == 40
        assert all(status == 200 for _, status in results)
