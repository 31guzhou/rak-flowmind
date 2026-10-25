"""图生视频协议 / 403 诊断 / 恢复幂等契约测试（ADR-126）。

运行（本机 venv）：

    PYTHONPATH=src python -m unittest tests.test_dsp_video_gen -v

覆盖：
- 单图 / 首尾帧 / 首尾同图 / 纯文生视频的 ``metadata.content`` 形状与 role；
- 提交前本地协议预检（缺 role、顺序错、非法 URL）；
- HTTP 403（空体 / HTML / JSON）诊断：原文不丢、脱敏、不可重试、global_permission；
- 任务级 400 ``role must be specified`` 的嵌套错误解析；
- 恢复运行 ``skipExisting`` 幂等判据。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from flowmind.config import DspConfig
from flowmind.skills import _dsp_video_gen as vg
from flowmind.skills.dsp_image_to_video import _should_reuse


def cfg() -> DspConfig:
    return DspConfig(videogen_model="doubao-seedance-2-0-fast", videogen_size="720x1280")


class _Resp:
    def __init__(self, status: int, text: str = "", *, content_type: str = "application/json"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type, "x-request-id": "req-test-1"}
        self._chunks = [self.content] if self.ok else []

    def json(self):
        return json.loads(self.text)

    def iter_content(self, size: int = 1 << 20):
        yield from self._chunks

    def close(self):
        pass


class PayloadContractTest(unittest.TestCase):
    def test_single_image_uses_content_with_first_frame_role(self):
        payload = vg.build_payload(
            cfg(), prompt="测试", image_url="https://x.test/a.png", seconds="4")
        content = payload["metadata"]["content"]
        self.assertEqual([c["type"] for c in content], ["text", "image_url"])
        self.assertEqual(content[1]["role"], "first_frame")
        self.assertEqual(content[1]["image_url"], {"url": "https://x.test/a.png"})
        self.assertNotIn("image", payload)
        self.assertNotIn("images", payload)
        self.assertEqual(payload["metadata"]["resolution"], "720p")
        self.assertEqual(payload["metadata"]["ratio"], "9:16")
        self.assertEqual(payload["duration"], 4)

    def test_first_last_frame_roles_and_adaptive_ratio(self):
        payload = vg.build_payload(
            cfg(), prompt="测试", image_url="https://x.test/a.png",
            tail_image_url="https://x.test/b.png", seconds="5")
        content = payload["metadata"]["content"]
        self.assertEqual([c.get("role") for c in content], [None, "first_frame", "last_frame"])
        self.assertEqual(payload["metadata"]["ratio"], "adaptive")

    def test_same_tail_falls_back_to_single_image(self):
        payload = vg.build_payload(
            cfg(), prompt="测试", image_url="https://x.test/a.png",
            tail_image_url="https://x.test/a.png", seconds="5")
        roles = [c.get("role") for c in payload["metadata"]["content"] if c["type"] == "image_url"]
        self.assertEqual(roles, ["first_frame"])

    def test_text_only_has_no_metadata_content(self):
        payload = vg.build_payload(cfg(), prompt="纯文生视频", image_url="", seconds="4")
        self.assertNotIn("metadata", payload)

    def test_validate_payload_rejects_missing_role(self):
        payload = {
            "model": "m", "prompt": "p", "duration": 4,
            "metadata": {"content": [
                {"type": "text", "text": "p"},
                {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
            ]},
        }
        problems = vg.validate_payload(payload)
        self.assertTrue(any("role" in p for p in problems), problems)

    def test_validate_payload_rejects_wrong_order_and_last_without_first(self):
        problems = vg.validate_payload({
            "model": "m", "prompt": "p", "duration": 4,
            "metadata": {"content": [
                {"type": "text", "text": "p"},
                {"type": "image_url", "image_url": {"url": "https://x.test/b.png"}, "role": "last_frame"},
            ]},
        })
        self.assertTrue(any("first_frame" in p for p in problems), problems)

    def test_validate_payload_rejects_non_http_url(self):
        problems = vg.validate_payload({
            "model": "m", "prompt": "p", "duration": 4,
            "metadata": {"content": [
                {"type": "image_url", "image_url": {"url": "file:///tmp/a.png"}, "role": "first_frame"},
            ]},
        })
        self.assertTrue(any("http" in p for p in problems), problems)


class DiagnosticTest(unittest.TestCase):
    def test_sanitize_redacts_bearer_sk_and_signed_query(self):
        text = ("Authorization: Bearer sk-abcdefghijklmnop "
                "url=https://bucket.r2.test/a.png?X-Amz-Signature=deadbeef&X-Amz-Credential=abc")
        out = vg.sanitize_text(text)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertNotIn("deadbeef", out)
        self.assertNotIn("X-Amz-Credential", out)
        self.assertIn("https://bucket.r2.test/a.png", out)

    def test_403_empty_body_is_environment_not_retriable(self):
        resp = _Resp(403, "", content_type="text/html")
        with mock.patch.object(vg.requests, "post", return_value=resp), \
                mock.patch.object(vg, "get_api_key", return_value="sk-test"):
            with self.assertRaises(vg.DspVideoGenError) as ctx:
                vg.submit_with_meta(
                    cfg(), prompt="p", image_url="https://x.test/a.png", seconds="4")
        err = ctx.exception
        self.assertEqual(err.status, 403)
        self.assertFalse(err.retriable)
        self.assertTrue(err.global_permission)
        self.assertEqual(err.category, "environment")
        self.assertEqual(err.request_id, "req-test-1")
        self.assertIn("响应体为空", str(err))

    def test_403_html_body_preserves_raw_excerpt(self):
        resp = _Resp(403, "<html><body>Cloudflare blocked</body></html>", content_type="text/html")
        with mock.patch.object(vg.requests, "post", return_value=resp), \
                mock.patch.object(vg, "get_api_key", return_value="sk-test"):
            with self.assertRaises(vg.DspVideoGenError) as ctx:
                vg.submit_with_meta(
                    cfg(), prompt="p", image_url="https://x.test/a.png", seconds="4")
        self.assertIn("Cloudflare blocked", ctx.exception.raw_excerpt)
        self.assertIn("Cloudflare blocked", str(ctx.exception))

    def test_429_is_transient_retriable(self):
        resp = _Resp(429, json.dumps({"error": {"code": "rate_limit_exceeded", "message": "slow"}}))
        with mock.patch.object(vg.requests, "post", return_value=resp), \
                mock.patch.object(vg, "get_api_key", return_value="sk-test"):
            with self.assertRaises(vg.DspVideoGenError) as ctx:
                vg.submit_with_meta(
                    cfg(), prompt="p", image_url="https://x.test/a.png", seconds="4")
        self.assertTrue(ctx.exception.retriable)
        self.assertEqual(ctx.exception.category, "transient")

    def test_task_level_400_role_message_is_parsed(self):
        raw = {
            "status": "failed",
            "error": 'HTTP 400: {"error":{"code":"InvalidParameter",'
                     '"message":"The parameter `content` specified in the request is not valid: '
                     'role must be specified for image contents."}}',
        }
        code, message, excerpt = vg._task_error_of(raw)
        self.assertEqual(code, "InvalidParameter")
        self.assertIn("role must be specified", message)
        self.assertIn("InvalidParameter", excerpt)

    def test_local_preflight_blocks_network_call(self):
        with mock.patch.object(vg.requests, "post") as post, \
                mock.patch.object(vg, "get_api_key", return_value="sk-test"):
            # 通过 monkeypatch 让 build_payload 产出一个缺 role 的坏请求
            with mock.patch.object(vg, "build_payload", return_value={
                "model": "m", "prompt": "p", "duration": 4,
                "metadata": {"content": [{"type": "image_url",
                                          "image_url": {"url": "https://x.test/a.png"}}]},
            }):
                with self.assertRaises(vg.DspVideoGenError) as ctx:
                    vg.submit_with_meta(
                        cfg(), prompt="p", image_url="https://x.test/a.png", seconds="4")
        post.assert_not_called()
        self.assertEqual(ctx.exception.stage, "preflight")


class DownloadFallbackTest(unittest.TestCase):
    def test_direct_403_falls_back_to_authenticated_content_endpoint(self):
        deny = _Resp(403, json.dumps({
            "Code": "AccessDenied", "Message": "Access Denied", "DetailErrCode": 14006,
        }))
        ok = _Resp(200, "", content_type="video/mp4")
        ok.content = b"fake-mp4-bytes"
        ok._chunks = [ok.content]
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            dest = f.name
        try:
            with mock.patch.object(vg.requests, "get", side_effect=[deny, ok]) as get, \
                    mock.patch.object(vg, "get_api_key", return_value="sk-test"):
                path, via = vg.fetch_result(
                    cfg(), "task-1", "https://tos.test/a.mp4", dest)
            self.assertEqual(via, "proxy")
            self.assertEqual(path, dest)
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"fake-mp4-bytes")
            # 第二次必须是带 Bearer 的代理端点
            self.assertEqual(get.call_count, 2)
            self.assertIn("/v1/videos/task-1/content", get.call_args_list[1].args[0])
            self.assertIn("Authorization", get.call_args_list[1].kwargs["headers"])
        finally:
            import os
            os.unlink(dest)

    def test_direct_success_does_not_call_proxy(self):
        ok = _Resp(200, "", content_type="video/mp4")
        ok._chunks = [b"x"]
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            dest = f.name
        try:
            with mock.patch.object(vg.requests, "get", return_value=ok) as get, \
                    mock.patch.object(vg, "get_api_key", return_value="sk-test"):
                _, via = vg.fetch_result(cfg(), "task-1", "https://tos.test/a.mp4", dest)
            self.assertEqual(via, "direct")
            self.assertEqual(get.call_count, 1)
        finally:
            import os
            os.unlink(dest)

    def test_both_channels_fail_preserve_both_errors(self):
        deny = _Resp(403, json.dumps({"Code": "AccessDenied", "Message": "Access Denied"}))
        with mock.patch.object(vg.requests, "get", side_effect=[deny, deny]), \
                mock.patch.object(vg, "get_api_key", return_value="sk-test"):
            with self.assertRaises(vg.DspVideoGenError) as ctx:
                vg.fetch_result(cfg(), "task-1", "https://tos.test/a.mp4", "unused.mp4")
        err = ctx.exception
        self.assertEqual(err.stage, "download")
        self.assertIn("direct", str(err))
        self.assertIn("proxy", str(err))
        self.assertIn("AccessDenied", err.raw_excerpt)
        self.assertTrue(err.global_permission)


class ResumeIdempotenceTest(unittest.TestCase):
    def test_skip_existing_wins_over_regenerate(self):
        self.assertTrue(_should_reuse(True, "s1", {"s1"}, True))
        self.assertFalse(_should_reuse(True, "s1", {"s1"}, False))
        self.assertTrue(_should_reuse(True, "s2", {"s1"}, False))
        self.assertFalse(_should_reuse(False, "s2", set(), True))

    def test_status_carries_provider_task_and_request_ids(self):
        from flowmind.skills.dsp_image_to_video import ShotVideoStatus
        status = ShotVideoStatus(
            shotId="s1", status="failed", reason="vendor_auth_failed",
            providerTaskId="", providerRequestId="req-1", attempts=1,
            errorCategory="environment", errorCode="permission_error",
            rawExcerpt="HTTP 403（响应体为空）")
        self.assertEqual(status.providerRequestId, "req-1")
        self.assertEqual(status.attempts, 1)
        self.assertEqual(status.errorCategory, "environment")


if __name__ == "__main__":
    unittest.main()
