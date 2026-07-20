"""投递层测试（P7）：邮件正文 Top5 提取、SMTP 组装、挑图管线。

不连真 SMTP、不跑 weasyprint（系统依赖重）：SMTP 用假 client 断言消息结构，
挑图用假 LLM + 假下载断言选择与落盘。PDF 渲染的真实性靠 CI 装库后的线上验收。
"""

from datetime import UTC, datetime

from src.config import RetryDefaults
from src.llm.client import ArkClient
from src.llm.settings import Settings
from src.models import NewsItem
from src.report.delivery import MailConfig, email_body_html, send_report
from src.report.images import _candidates, attach_images
from tests.test_llm import FakeOpenAI, _response

RETRY = RetryDefaults(max_retries=1)
WEEKLY_MD = """# AI 前沿周报 · 2026-07-19

## 本周 Top5

### 1. LongStraw: Long-Context RL
分析一。

### 2. GPT-5.6 Sol 预览
分析二。

## 本周趋势
趋势正文。
"""


class TestEmailBody:
    def test_extracts_top5_titles(self):
        html = email_body_html(WEEKLY_MD)
        assert "AI 前沿周报 · 2026-07-19" in html
        assert "1. LongStraw: Long-Context RL" in html
        assert "2. GPT-5.6 Sol 预览" in html
        assert "趋势正文" not in html  # 只放 Top5，不塞正文段落

    def test_no_top5_falls_back(self):
        html = email_body_html("# 报告\n\n## 尾注\n- x")
        assert "详见附件 PDF" in html


class TestMailConfig:
    def test_disabled_without_host_or_password(self):
        assert not MailConfig(
            host="", port=465, user="u", password="", sender="s", recipient="r"
        ).enabled

    def test_enabled_with_full_config(self):
        assert MailConfig(
            host="smtp.gmail.com", port=465, user="u", password="pw",
            sender="s@x.com", recipient="r@x.com",
        ).enabled


class _FakeSMTP:
    def __init__(self):
        self.logged_in = False
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        self.sent = msg


class TestSendReport:
    def test_message_structure_and_attachment(self):
        cfg = MailConfig(
            host="smtp.x.com", port=465, user="u@x.com", password="pw",
            sender="u@x.com", recipient="to@x.com",
        )
        fake = _FakeSMTP()
        send_report(
            cfg, "周报 · 2026-07-19", "<h2>hi</h2>", b"%PDF-1.4 fake", "weekly.pdf",
            smtp_factory=lambda: fake,
        )
        assert fake.logged_in and fake.sent is not None
        msg = fake.sent
        assert msg["Subject"] == "周报 · 2026-07-19"
        assert msg["To"] == "to@x.com"
        attachments = [p for p in msg.iter_attachments()]
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "weekly.pdf"
        assert attachments[0].get_content_type() == "application/pdf"


def _item() -> NewsItem:
    return NewsItem.create(
        source="hf-blog", title="某深读", url="https://x.com/a",
        published_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


class TestImages:
    def test_candidates_filters_junk(self):
        md = (
            "![架构图：整体流程](https://cdn.x.com/arch.png)\n"
            "![](https://cdn.x.com/nocaption.png)\n"  # 无 caption
            "![logo](https://cdn.x.com/logo.svg)\n"  # svg 非位图
            "![结果对比图表](https://cdn.x.com/results.jpg)\n"
        )
        cands = _candidates(md)
        assert [c for c, _ in cands] == ["架构图：整体流程", "结果对比图表"]

    def test_attach_picks_and_downloads(self, monkeypatch, tmp_path):
        md = (
            "![架构图](https://cdn.x.com/arch.png)\n"
            "![结果图](https://cdn.x.com/res.png)\n"
        )
        # 模型挑第 0 张
        fake = FakeOpenAI([_response('{"indices": [0]}')])
        client = ArkClient(settings=Settings(ark_api_key="k"), client=fake)
        monkeypatch.setattr(
            "src.report.images.base.fetch_bytes", lambda url, retry: b"x" * 5000
        )
        item = _item()
        attach_images(client, item, md, tmp_path / "assets", RETRY)
        assert len(item.images) == 1
        assert item.images[0]["caption"] == "架构图"
        assert item.images[0]["path"].startswith("assets/")
        assert (tmp_path / "assets" / item.images[0]["path"].split("/")[1]).exists()

    def test_out_of_range_index_skipped(self, monkeypatch, tmp_path):
        md = "![架构图](https://cdn.x.com/arch.png)\n"
        fake = FakeOpenAI([_response('{"indices": [5]}')])  # 越界序号
        client = ArkClient(settings=Settings(ark_api_key="k"), client=fake)
        monkeypatch.setattr(
            "src.report.images.base.fetch_bytes", lambda url, retry: b"x" * 5000
        )
        item = _item()
        attach_images(client, item, md, tmp_path / "assets", RETRY)
        assert item.images == []  # 编造序号不塞错图

    def test_tiny_image_rejected(self, monkeypatch, tmp_path):
        md = "![架构图](https://cdn.x.com/arch.png)\n"
        fake = FakeOpenAI([_response('{"indices": [0]}')])
        client = ArkClient(settings=Settings(ark_api_key="k"), client=fake)
        monkeypatch.setattr(
            "src.report.images.base.fetch_bytes", lambda url, retry: b"tiny"
        )
        item = _item()
        attach_images(client, item, md, tmp_path / "assets", RETRY)
        assert item.images == []  # <3KB 判为图标，不配
