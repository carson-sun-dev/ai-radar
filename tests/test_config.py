"""配置加载测试：真实 sources.yaml 必须可加载，错误配置必须在加载期炸出来。"""

import pytest
from pydantic import ValidationError

from src.config import CollectorKind, RadarConfig, load_config


def test_real_sources_yaml_loads():
    # 仓库里的 sources.yaml 是生产配置，跑测试就是在校验它
    config = load_config("sources.yaml")
    ids = [s.id for s in config.sources]
    assert len(ids) == len(set(ids))
    # 设计定案的关键源必须在位：少一个说明配置被误删
    for required in ["openai-news", "anthropic-engineering", "arxiv-papers", "hf-daily-papers"]:
        assert required in ids
    # 10 家机构 + arXiv 通道
    assert len({s.org for s in config.sources}) == 11


def test_missing_target_field_fails_at_load():
    # rss 源没写 url：必须在加载期报错，而不是采集时 NoneType 崩溃
    with pytest.raises(ValidationError, match="url"):
        RadarConfig.model_validate(
            {"sources": [{"id": "bad", "org": "X", "collector": "rss"}]}
        )


def test_github_collector_needs_repo_or_org():
    with pytest.raises(ValidationError, match="repo 或 github_org"):
        RadarConfig.model_validate(
            {"sources": [{"id": "bad", "org": "X", "collector": "github"}]}
        )


def test_duplicate_ids_rejected():
    src = {"id": "dup", "org": "X", "collector": "rss", "url": "https://a.com/feed"}
    with pytest.raises(ValidationError, match="重复"):
        RadarConfig.model_validate({"sources": [src, dict(src)]})


def test_backoff_defaults_match_design():
    # 设计纪要第 11 节定案的 5/15/45 秒：改动必须是有意的（会挂测试）
    config = load_config("sources.yaml")
    d = config.defaults
    delays = [d.backoff_base_seconds * d.backoff_factor**n for n in range(d.max_retries)]
    assert delays == [5, 15, 45]
    assert CollectorKind.WEB in {s.collector for s in config.sources}
