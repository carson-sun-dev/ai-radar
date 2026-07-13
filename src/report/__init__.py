"""报告层：markdown/JSON 落盘、PDF 渲染（weasyprint）、SMTP 邮件。

一份报告两个受众（设计纪要第 6 节）：YAML frontmatter 供机器（实体索引/历史关联），
正文 markdown 供人读。GitHub 存 md，邮件发 PDF 附件。
PDF 渲染的已知坑：CI runner 需先装 fonts-noto-cjk，否则中文全是方块。
"""
