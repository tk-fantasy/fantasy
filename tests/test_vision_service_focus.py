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

    def test_get_all_focuses_flat(self):
        """get_all_focuses_flat 拍平所有 bucket,每条含 camera_id(持久化用)。"""
        vs = VisionService(client=MagicMock())
        vs.add_focus("人", camera_id="cam_a")
        vs.add_focus("车", camera_id="cam_b")
        vs.add_focus("桌", camera_id="cam_a")
        flat = vs.get_all_focuses_flat()
        assert len(flat) == 3
        # 每条都有 camera_id
        cids = {f["camera_id"] for f in flat}
        assert cids == {"cam_a", "cam_b"}
        # 拍平后可以 load_focuses 重新分桶(持久化往返)
        vs2 = VisionService(client=MagicMock())
        vs2.load_focuses(flat)
        assert len(vs2.get_vision_focuses(camera_id="cam_a")) == 2
        assert len(vs2.get_vision_focuses(camera_id="cam_b")) == 1


class TestGetVisionFocusLegacy:
    """get_vision_focus() 兼容旧单条 API：曾因整数索引 dict 导致 KeyError。

    _vision_focuses 是 {camera_id: [items]}，旧代码 self._vision_focuses[0]
    在任何非空状态下必抛 KeyError（settings_routes:722/735 调用即 500）。
    """

    def test_empty_returns_default(self):
        vs = VisionService(client=MagicMock())
        assert vs.get_vision_focus() == "画面中的人和他们的行为"

    def test_non_empty_returns_first_bucket_first_item_text(self):
        """非空时取第一个 bucket 的第一项 text（不再 KeyError）。"""
        vs = VisionService(client=MagicMock())
        vs.add_focus("车", camera_id="cam_b")
        vs.add_focus("人", camera_id="cam_a")
        # cam_b 先插入 → 第一个 bucket → 返回 "车"
        assert vs.get_vision_focus() == "车"

    def test_after_load_focuses_returns_first_item(self):
        """load_focuses 后同样能正确返回（复现 settings_routes 启动加载路径）。"""
        vs = VisionService(client=MagicMock())
        vs.load_focuses([
            {"id": "f1", "text": "门口来人", "enabled": True, "camera_id": "cam_a"},
        ])
        assert vs.get_vision_focus() == "门口来人"
