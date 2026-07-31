"""VisionService per-camera focuses 单测(Task 2.10)。

验证 _vision_focuses 从全局 list 改为 dict[camera_id → list],
各路摄像头的关注项互相隔离。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.vision_service import VisionService


class TestPerCameraFocuses:
    def test_focuses_isolated_per_camera(self):
        vs = VisionService(client=MagicMock())
        vs.add_focus("人", camera_id="cam_a")
        vs.add_focus("车", camera_id="cam_b")
        a = vs.get_vision_focuses(camera_id="cam_a")
        b = vs.get_vision_focuses(camera_id="cam_b")
        assert len(a) == 1 and a[0]["text"] == "人"
        assert len(b) == 1 and b[0]["text"] == "车"

    def test_delete_one_camera_does_not_affect_other(self):
        vs = VisionService(client=MagicMock())
        vs.add_focus("人", camera_id="cam_a")
        vs.add_focus("车", camera_id="cam_b")
        a = vs.get_vision_focuses(camera_id="cam_a")
        # 删 a 的不影响 b
        vs.delete_focus(a[0]["id"], camera_id="cam_a")
        assert vs.get_vision_focuses(camera_id="cam_a") == []
        assert len(vs.get_vision_focuses(camera_id="cam_b")) == 1

    def test_update_focus_scoped_to_camera(self):
        vs = VisionService(client=MagicMock())
        # 同 id 在不同路各自独立
        vs.add_focus("旧", camera_id="cam_a")
        vs.add_focus("旧", camera_id="cam_b")
        # 两路都有 id 相同的 focus(概率极低但测隔离)
        a_id = vs.get_vision_focuses(camera_id="cam_a")[0]["id"]
        b_id = vs.get_vision_focuses(camera_id="cam_b")[0]["id"]
        # 更新 cam_a 的只影响 cam_a
        vs.update_focus(a_id, text="新", camera_id="cam_a")
        a = vs.get_vision_focuses(camera_id="cam_a")
        b = vs.get_vision_focuses(camera_id="cam_b")
        assert a[0]["text"] == "新"
        assert b[0]["text"] == "旧"

    def test_load_focuses_partitions_by_camera_id(self):
        """load_focuses 从 KV 加载(每条已含 camera_id),按 camera_id 分桶。"""
        vs = VisionService(client=MagicMock())
        kv_data = [
            {"id": "f1", "text": "人", "enabled": True, "camera_id": "cam_a"},
            {"id": "f2", "text": "门", "enabled": True, "camera_id": "cam_b"},
            {"id": "f3", "text": "桌", "enabled": False, "camera_id": "cam_a"},
        ]
        vs.load_focuses(kv_data)
        a = vs.get_vision_focuses(camera_id="cam_a")
        b = vs.get_vision_focuses(camera_id="cam_b")
        assert len(a) == 2
        assert len(b) == 1

    def test_default_camera_id_is_empty_string(self):
        """不传 camera_id 时归到空串桶(全局,向后兼容)。"""
        vs = VisionService(client=MagicMock())
        vs.add_focus("全局关注")
        assert len(vs.get_vision_focuses()) == 1
        assert vs.get_vision_focuses()[0]["text"] == "全局关注"

    def test_combined_focus_per_camera(self):
        """_get_combined_focus 按摄像头拼接 enabled 项。"""
        vs = VisionService(client=MagicMock())
        vs.add_focus("人", camera_id="cam_a")
        vs.add_focus("车", camera_id="cam_b")
        assert vs._get_combined_focus(camera_id="cam_a") == "人"
        assert vs._get_combined_focus(camera_id="cam_b") == "车"
