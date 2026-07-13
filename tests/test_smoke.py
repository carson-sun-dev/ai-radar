"""P0 冒烟测试：只保证 CI 流水线本身可用，后续阶段的真实测试会逐步替代它。"""


def test_packages_importable():
    # 骨架阶段唯一要保证的事：所有子包能被正常 import（包路径/命名没配错）
    import src.collectors
    import src.llm
    import src.pipeline
    import src.report
    import src.tools
    import src.validate

    assert all([src.collectors, src.pipeline, src.tools, src.llm, src.report, src.validate])
