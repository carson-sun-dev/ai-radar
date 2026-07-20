"""投递层：报告 md → HTML → PDF（weasyprint），SMTP 发信（设计纪要第 6 节）。

分工定案：GitHub 存 markdown（版本可追溯），邮件发 PDF 附件（离线可读、格式稳定）。
邮件正文放 Top5 标题（HTML），手机扫一眼就能决定是否打开附件。

依赖注入：SMTP 发送函数可替换（测试不连真服务器）；weasyprint 的系统依赖
（pango/cairo + fonts-noto-cjk）在 CI 里 apt 装，本地缺库时 render_pdf 会报错——
这是环境问题不是代码问题，delivery 节点捕获后写警告、不炸整条流水线。
"""

import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import markdown as md_lib

# CJK 字体显式声明：CI 装了 fonts-noto-cjk 但 weasyprint 不会自动优先中文字体，
# 不点名就可能回退到无 CJK 字形的默认字体 → 满页方块（设计纪要第 6 节的坑）
_PDF_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: "Noto Sans CJK SC", "Noto Sans", sans-serif;
       line-height: 1.6; font-size: 11pt; color: #222; }
h1 { font-size: 20pt; border-bottom: 2px solid #333; padding-bottom: 4px; }
h2 { font-size: 15pt; margin-top: 1.2em; color: #1a1a1a; }
h3 { font-size: 12pt; margin-top: 1em; }
blockquote { color: #666; border-left: 3px solid #ccc; padding-left: 10px; font-size: 10pt; }
a { color: #0645ad; text-decoration: none; }
img { max-width: 100%; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }
"""

_H3_NUM = re.compile(r"^### (\d+)\.\s+(.+)$", re.M)  # 周报 Top5 的编号标题


@dataclass(frozen=True)
class MailConfig:
    """SMTP 配置。enabled=False（缺 host/密码）时投递节点只渲染 PDF 不发信。"""

    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.password and self.recipient)

    @classmethod
    def from_env(cls) -> "MailConfig":
        user = os.environ.get("SMTP_USER", "").strip()
        return cls(
            host=os.environ.get("SMTP_HOST", "").strip(),
            port=int(os.environ.get("SMTP_PORT", "465")),
            user=user,
            password=os.environ.get("SMTP_PASSWORD", "").strip(),
            sender=os.environ.get("SMTP_SENDER", user).strip(),
            recipient=os.environ.get("EMAIL_TO", "").strip(),
        )


def render_pdf(report_md: str, base_url: str | None = None) -> bytes:
    """md → HTML → PDF。base_url 让 assets/ 相对图片路径能被 weasyprint 解析。"""
    # 延迟导入：weasyprint 拉起一堆系统库，未装环境（如纯单测）不该在 import 期就崩
    from weasyprint import HTML

    html_body = md_lib.markdown(report_md, extensions=["tables", "fenced_code"])
    html_doc = (
        f"<html><head><meta charset='utf-8'><style>{_PDF_CSS}</style></head>"
        f"<body>{html_body}</body></html>"
    )
    return HTML(string=html_doc, base_url=base_url).write_pdf()


def email_body_html(report_md: str) -> str:
    """邮件正文：报告一级标题 + Top5 标题清单（手机速览用）。"""
    title_match = re.search(r"^# (.+)$", report_md, re.M)
    title = title_match.group(1) if title_match else "AI 前沿报告"
    items = _H3_NUM.findall(report_md)
    lines = "".join(f"<li>{num}. {t}</li>" for num, t in items[:5])
    body = f"<ol>{lines}</ol>" if lines else "<p>详见附件 PDF。</p>"
    return (
        f"<h2>{title}</h2><p>本期 Top5：</p>{body}"
        "<p style='color:#888;font-size:12px'>完整内容见附件 PDF。</p>"
    )


def send_report(
    config: MailConfig,
    subject: str,
    html_body: str,
    pdf_bytes: bytes,
    pdf_name: str,
    smtp_factory=None,
) -> None:
    """发一封带 PDF 附件的 HTML 邮件。smtp_factory 可注入（测试不连真服务器）。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = config.recipient
    msg.set_content("本邮件为 HTML 格式，Top5 标题与 PDF 附件见富文本版本。")
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name
    )
    # 默认 SMTPS（465，隐式 TLS）——Gmail 应用专用密码走这条最省事
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(config.host, config.port))
    with factory() as smtp:
        if config.user:
            smtp.login(config.user, config.password)
        smtp.send_message(msg)
