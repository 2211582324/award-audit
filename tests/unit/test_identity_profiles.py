"""附件 5 示例驱动的统一身份规则。"""

from __future__ import annotations

import pytest

from award_audit.agent.integration import derive_audit_case_input
from award_audit.core.identity import (
    IDENTITY_VERSION,
    build_business_identity_key,
    build_profile_identity,
)
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import IDENTITY_PROFILES, resolve_identity_profile
from award_audit.core.pipeline.checks import l4_dedup
from award_audit.core.pipeline.dedup import dedup_key
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.template_registry import build_template_spec


def _imported(
    table_code: str,
    codes: list[str],
    rows: list[dict[str, str]],
    *,
    year: str = "2022",
) -> ImportedFile:
    return ImportedFile(
        batch="附件5规则测试",
        path=f"/tmp/{table_code}-示例-{year}.xlsx",
        file_name=f"{table_code}-示例-{year}.xlsx",
        claimed_table_code=table_code,
        award_name="示例",
        year=year,
        sheet_name="数据",
        header_codes=codes,
        header_names=codes,
        rows=[[row.get(code, "") for code in codes] for row in rows],
    )


@pytest.mark.parametrize(
    ("suffix", "primary_alternatives"),
    [
        ("MKPTKC", [["KCMC"]]),
        ("JXKYJL", [["XMBH"], ["XMCG"]]),
        ("KYXM", [["XMBH"], ["XMMC"]]),
        ("GXDJSCGR", [["XRYXM"]]),
        ("GXDJSCJT", [["ZBMC"]]),
        ("KCTX", [["KCMC"]]),
        ("XSJSHJ", [["ZPBH"], ["ZPMC"], ["CSDWMC"]]),
        ("XWLWHJ", [["LWTM", "ZZXM"]]),
        ("YXJC", [["JCMC"]]),
        ("DXPM", [["XXDM"], ["XDWMC"], ["XXMC_YW", "GBM"]]),
        (
            "XKPM",
            [
                ["XXDM", "XKMC"],
                ["XDWMC", "XKMC"],
                ["XXMC_YW", "XKMC_YW", "GBM"],
            ],
        ),
        ("RZXX", [["XDWMC", "TGRZZY"]]),
        ("RCCH", [["XRYXM"]]),
        ("XSTD", [["YJFX", "XDTRXM", "XDWMC"]]),
        ("ZCPT", [["PTMC"]]),
        ("JJCGWK", [["XMMC"]]),
        ("QK", [["QKMC", "CBDW"]]),
    ],
)
def test_all_standard_profiles_lock_sample_driven_primary_identity(
    suffix: str, primary_alternatives: list[list[str]]
) -> None:
    profile = IDENTITY_PROFILES[suffix]

    assert profile.primary_alternatives == primary_alternatives
    assert all("ZYLBM" not in fields for fields in primary_alternatives)
    assert all(
        not any(field in profile.scope_fields for field in fields)
        for fields in primary_alternatives
    )


def test_mkptkc_context_is_not_a_row_match_identity() -> None:
    table_code = "CON_GG_XK_KCJX_MKPTKC"
    codes = ["ZYLBM", "ZYLB", "XDWMC", "KCMC", "XFZR", "KKPT", "PZNY", "CJSJ", "BZ"]
    spec = build_template_spec(table_code, "慕课平台课程", codes, codes)
    profile = resolve_identity_profile(spec)

    assert IDENTITY_VERSION == "identity-v2"
    assert profile.scope_fields == ["ZYLBM", "PZNY"]
    assert profile.primary_alternatives == [["KCMC"]]
    assert profile.submit_cols == ["KCMC"]
    assert "ZYLBM" not in profile.submit_cols
    assert "PZNY" not in profile.submit_cols

    first = build_profile_identity({
        "ZYLBM": "03010010",
        "PZNY": "2022年",
        "KCMC": "植物学",
        "XFZR": "陈红芝",
        "XDWMC": "新乡工程学院",
        "KKPT": "中国大学MOOC",
    }, profile)
    assert first is not None
    assert first.key == "植物学"
    assert first.fields == ("KCMC",)


def test_mkptkc_l4_uses_course_identity_and_reports_collision() -> None:
    table_code = "CON_GG_XK_KCJX_MKPTKC"
    codes = ["ZYLBM", "ZYLB", "XDWMC", "KCMC", "XFZR", "KKPT", "PZNY", "CJSJ", "BZ"]
    spec = build_template_spec(table_code, "慕课平台课程", codes, codes)
    base = {
        "ZYLBM": "03010010", "ZYLB": "课程", "XDWMC": "新乡工程学院",
        "KKPT": "中国大学MOOC", "PZNY": "2022年", "CJSJ": "202212",
    }
    imported = _imported(table_code, codes, [
        {**base, "KCMC": "植物学", "XFZR": "陈红芝", "BZ": "理学"},
        {**base, "KCMC": "毛泽东思想和中国特色社会主义理论体系概论", "XFZR": "马艳红;郭厚顺", "BZ": "法学"},
        {**base, "KCMC": "植物学", "XFZR": "另一负责人", "BZ": "理学"},
    ])

    issues = l4_dedup.run(imported, spec)
    assert not any(item.rule_id == "L4-01" for item in issues)
    assert any(item.rule_id == "L4-03" and item.row == 3 for item in issues)


def test_mkptkc_exact_repeat_is_duplicate_regardless_of_context_constants() -> None:
    table_code = "CON_GG_XK_KCJX_MKPTKC"
    codes = ["ZYLBM", "ZYLB", "XDWMC", "KCMC", "XFZR", "KKPT", "PZNY"]
    spec = build_template_spec(table_code, "慕课平台课程", codes, codes)
    same = {
        "ZYLBM": "03010010", "ZYLB": "课程", "XDWMC": "新乡工程学院",
        "KCMC": "植物学", "XFZR": "陈红芝", "KKPT": "中国大学MOOC", "PZNY": "2022年",
    }
    imported = _imported(table_code, codes, [same, dict(same)])

    assert dedup_key(imported, 0, spec) == dedup_key(imported, 1, spec)
    assert any(item.rule_id == "L4-01" for item in l4_dedup.run(imported, spec))


def test_xsjshj_uses_ordered_real_data_identity_alternatives() -> None:
    xsjshj = build_template_spec(
        "CON_GG_XK_RCPY_XSJSHJ",
        "学生竞赛获奖",
        ["ZYLBM", "ZPBH", "ZPMC", "CSDWMC", "FZRXM", "CSZZMD", "XCSDW", "SDLB", "HJNF", "BSJS", "XRYXM"],
        ["ZYLBM", "ZPBH", "ZPMC", "CSDWMC", "FZRXM", "CSZZMD", "XCSDW", "SDLB", "HJNF", "BSJS", "XRYXM"],
    )
    xstd = build_template_spec(
        "CON_GG_XK_SZDW_XSTD",
        "学术团队",
        ["ZYLBM", "BH", "TDMC", "PXND", "YJFX", "XDTRXM", "XDWMC", "ZZJE"],
        ["ZYLBM", "BH", "TDMC", "PXND", "YJFX", "XDTRXM", "XDWMC", "ZZJE"],
    )

    competition = resolve_identity_profile(xsjshj)
    team = resolve_identity_profile(xstd)
    assert competition.primary_alternatives == [["ZPBH"], ["ZPMC"], ["CSDWMC"]]
    assert competition.submit_cols == ["ZPBH", "ZPMC", "CSDWMC"]
    assert "XRYXM" not in competition.submit_cols
    assert team.primary_alternatives == [["YJFX", "XDTRXM", "XDWMC"]]
    assert team.submit_cols == ["YJFX", "XDTRXM", "XDWMC"]
    assert "BH" not in team.submit_cols and "TDMC" not in team.submit_cols


def test_xstd_blank_composite_identity_falls_back_to_registered_title() -> None:
    table_code = "CON_GG_XK_SZDW_XSTD"
    codes = ["ZYLBM", "TDMC", "YJFX", "XDTRXM", "XDWMC"]
    spec = build_template_spec(table_code, "学术团队", codes, codes)
    imported = _imported(table_code, codes, [{
        "ZYLBM": "02040005",
        "TDMC": "天然药物学教师团队",
        "YJFX": "",
        "XDTRXM": "屠鹏飞",
        "XDWMC": "北京大学",
    }], year="2025")

    context = derive_audit_case_input(
        {"resource_code": "02040005", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    assert context.identity_primary_alternatives == [["TDMC", "XDWMC"]]
    assert context.match_fields == ["TDMC", "XDWMC"]
    assert context.attachment_match_fields == ["TDMC", "XDWMC"]
    assert context.match_combine == "all"
    assert len(context.role_scopes) == 1
    assert context.role_scopes[0]["submitted_identity_count"] == 1
    assert set(context.role_scopes[0]["submitted_identities"].values()) == {
        "天然药物学教师团队;北京大学"
    }
    assert context.row_conservation["assigned_rows"] == 1


def test_xsjshj_team_roster_derives_team_identity_for_m4_and_m5(tmp_path) -> None:
    table_code = "CON_GG_XK_RCPY_XSJSHJ"
    codes = [
        "ZYLBM", "ZYLB", "ZPBH", "ZPMC", "CSDWMC", "XCSDW", "HJDJ",
        "HJNF", "BSJS",
    ]
    spec = build_template_spec(table_code, "学生竞赛获奖信息", codes, codes)
    path = tmp_path / "team-roster.xlsx"
    path.touch()
    imported = _imported(table_code, codes, [
        {
            "ZYLBM": "04030052",
            "ZYLB": "全国研究生渔菁英挑战赛",
            "CSDWMC": "摸鱼先锋队",
            "XCSDW": "大连海洋大学",
            "HJDJ": "一等奖",
            "HJNF": "2025",
            "BSJS": "第四届",
        },
        {
            "ZYLBM": "04030052",
            "ZYLB": "全国研究生渔菁英挑战赛",
            "CSDWMC": "防饲菁英队",
            "XCSDW": "广东海洋大学",
            "HJDJ": "一等奖",
            "HJNF": "2025",
            "BSJS": "第四届",
        },
    ], year="2025").model_copy(update={"path": str(path), "file_name": path.name})

    profile = resolve_identity_profile(spec)
    first_row = {
        code: imported.value(0, code)
        for code in imported.header_codes
    }
    identity = build_profile_identity(first_row, profile)
    assert identity is not None
    assert identity.fields == ("CSDWMC",)
    assert identity.display == "摸鱼先锋队"

    context = derive_audit_case_input(
        {"resource_code": "04030052", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )
    assert context.identity_primary_alternatives == [["CSDWMC"]]
    assert context.match_fields == ["CSDWMC"]
    assert context.attachment_match_fields == ["CSDWMC", "XCSDW", "HJDJ"]


def test_kyxm_missing_number_and_name_stays_unidentified() -> None:
    table_code = "CON_GG_XK_KYXM"
    codes = [
        "ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "XFZRXM",
        "XDWMC",
    ]
    spec = build_template_spec(table_code, "项目信息", codes, codes)
    profile = resolve_identity_profile(spec)
    row = {
        "ZYLBM": "02010001",
        "ZYLB": "组织类事项",
        "LXNF": "2024",
        "XMBH": "",
        "XMMC": "",
        "XMLB": "组织奖",
        "XFZRXM": "张三",
        "XDWMC": "示例单位",
    }
    imported = _imported(table_code, codes, [row], year="2024")

    assert build_profile_identity(row, profile) is None
    assert any(item.rule_id == "L4-04" for item in l4_dedup.run(imported, spec))


def test_qk_and_xstd_examples_produce_distinct_business_keys() -> None:
    qk_code = "CON_GG_XK_XSCG_QK"
    qk_codes = ["ZYLBM", "BZ", "QKMC", "CBDW", "CN", "ISSN", "SLNF"]
    qk_spec = build_template_spec(qk_code, "期刊", qk_codes, qk_codes)
    qk = _imported(qk_code, qk_codes, [
        {"ZYLBM": "5080012", "BZ": "领军期刊", "QKMC": "分子植物", "CBDW": "中国科学院大学", "SLNF": "2019"},
        {"ZYLBM": "5080012", "BZ": "领军期刊", "QKMC": "分子植物", "CBDW": "中国工程院战略咨询中心", "SLNF": "2019"},
    ], year="2019")

    team_code = "CON_GG_XK_SZDW_XSTD"
    team_codes = ["ZYLBM", "PXND", "YJFX", "ZZJE", "XDTRXM", "XDWMC", "BH", "TDMC"]
    team_spec = build_template_spec(team_code, "学术团队", team_codes, team_codes)
    teams = _imported(team_code, team_codes, [
        {"ZYLBM": "2040004", "PXND": "2017", "YJFX": "代谢性高血压的发病机制及其综合干预", "ZZJE": "1050", "XDTRXM": "祝之明", "XDWMC": "第三军医大学"},
        {"ZYLBM": "2040004", "PXND": "2017", "YJFX": "核酸识别和调控的化学生物学研究", "ZZJE": "1050", "XDTRXM": "周翔", "XDWMC": "武汉大学"},
    ], year="2017")

    assert dedup_key(qk, 0, qk_spec) != dedup_key(qk, 1, qk_spec)
    assert dedup_key(teams, 0, team_spec) != dedup_key(teams, 1, team_spec)
    assert not any(item.rule_id == "L4-01" for item in l4_dedup.run(qk, qk_spec))
    assert not any(item.rule_id == "L4-01" for item in l4_dedup.run(teams, team_spec))


def test_m5_context_is_derived_from_the_same_profile(tmp_path) -> None:
    table_code = "CON_GG_XK_KCJX_MKPTKC"
    codes = ["ZYLBM", "ZYLB", "XDWMC", "KCMC", "XFZR", "KKPT", "PZNY"]
    spec = build_template_spec(table_code, "慕课平台课程", codes, codes)
    path = tmp_path / "course.xlsx"
    path.touch()
    imported = _imported(table_code, codes, [{
        "ZYLBM": "03010010", "ZYLB": "课程", "XDWMC": "新乡工程学院",
        "KCMC": "植物学", "XFZR": "陈红芝", "KKPT": "中国大学MOOC", "PZNY": "2022年",
    }])
    imported = imported.model_copy(update={"path": str(path), "file_name": path.name})

    context = derive_audit_case_input(
        {"resource_code": "03010010", "year": "2022"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    assert context.identity_version == "identity-v2"
    assert context.match_fields == ["KCMC"]
    assert context.attachment_match_fields == ["KCMC", "XDWMC", "KKPT", "XFZR"]
    assert context.identity_primary_alternatives == [["KCMC"]]
    assert context.identity_scope_fields == ["ZYLBM", "PZNY"]


def test_current_keys_recomputes_identity_v2_without_rewriting_history(tmp_path) -> None:
    table_code = "CON_GG_XK_KCJX_MKPTKC"
    codes = ["ZYLBM", "PZNY", "KCMC", "XDWMC", "KKPT", "XFZR"]
    spec = build_template_spec(table_code, "慕课平台课程", codes, codes)
    row = {
        "ZYLBM": "03010010",
        "PZNY": "2022年",
        "KCMC": "植物学",
        "XDWMC": "新乡工程学院",
        "KKPT": "中国大学MOOC",
        "XFZR": "陈红芝",
    }
    expected = build_business_identity_key(row, resolve_identity_profile(spec))
    store = Store(tmp_path / "identity-history.db")
    try:
        store._insert_record("legacy-v1-key", table_code, row, None, "test")
        store.conn.commit()

        assert store.current_keys() == {"legacy-v1-key"}
        assert store.current_keys({table_code: spec}) == {"legacy-v1-key", expected}
        historical = store.conn.execute(
            "SELECT business_key FROM record WHERE is_current=1"
        ).fetchone()
        assert historical is not None and historical["business_key"] == "legacy-v1-key"
    finally:
        store.close()


def test_all_17_templates_expose_role_profiles() -> None:
    assert len(IDENTITY_PROFILES) == 17
    assert all(profile.role_profiles for profile in IDENTITY_PROFILES.values())
    assert {item.role_type for item in IDENTITY_PROFILES["XSJSHJ"].role_profiles} == {
        "team", "instructor_or_person", "organization", "ranking_or_special",
    }


def test_student_competition_uses_unique_team_identity_denominator() -> None:
    table_code = "CON_GG_XK_XSJSHJ"
    codes = [
        "ZYLBM", "ZYLB", "HJNF", "BSJS", "SDLB", "ZPBH", "ZPMC",
        "CSDWMC", "FZRXM", "XRYXM", "XCSDW", "XDWMC", "HJDJ",
    ]
    rows = []
    for index in range(120):
        team_index = index if index < 83 else index - 83
        rows.append({
            "ZYLBM": "04030052", "ZYLB": "竞赛", "HJNF": "2025",
            "BSJS": "全国决赛", "SDLB": "开源创新",
            "CSDWMC": f"队伍{team_index:03d}", "XCSDW": "示例大学",
            "HJDJ": "参赛队伍奖",
        })
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "学生竞赛获奖", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "04030052", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    team_scope = next(item for item in context.role_scopes if item["role_type"] == "team")
    assert team_scope["submitted_row_count"] == 120
    assert team_scope["submitted_identity_count"] == 83
    assert context.expected_scope_count == 83


def test_china_patent_rows_are_conserved_across_project_organization_and_person_roles() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = [
        "ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "XFZRXM",
        "XCYRXM", "XDWMC",
    ]
    rows = [
        {
            "ZYLBM": "06020007", "ZYLB": "中国专利奖", "LXNF": "2025",
            "XMBH": "ZL-1", "XMMC": "项目一", "XMLB": "中国专利金奖项目",
            "XDWMC": "项目单位",
        },
        {
            "ZYLBM": "06020007", "ZYLB": "中国专利奖", "LXNF": "2025",
            "XMLB": "最佳组织奖", "XDWMC": "组织单位",
        },
        {
            "ZYLBM": "06020007", "ZYLB": "中国专利奖", "LXNF": "2025",
            "XMLB": "最佳推荐奖", "XCYRXM": "推荐人",
        },
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "中国专利奖", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "06020007", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    assert context.row_conservation["total_rows"] == 3
    assert context.row_conservation["assigned_rows"] == 3
    assert context.row_conservation["ambiguous_rows"] == 0
    assert context.row_conservation["unassigned_rows"] == 0
    recommendation = next(
        scope for scope in context.role_scopes
        if scope["business_scope"].get("XMLB") == "最佳推荐奖"
    )
    assert recommendation["role_type"] == "instructor_or_person"
    assert recommendation["submitted_identity_count"] == 1
    assert list(recommendation["submitted_identities"].values()) == ["推荐人"]


def test_duplicate_project_titles_keep_person_and_organization_discriminators() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = [
        "ZYLBM", "ZYLB", "LXNF", "XMMC", "XMLB", "XFZRXM", "XDWMC",
    ]
    rows = [
        {
            "ZYLBM": "05040003", "ZYLB": "教育部人文社科", "LXNF": "2025",
            "XMMC": "生成式人工智能对大学生就业的影响及对策研究",
            "XMLB": "青年基金项目", "XFZRXM": person, "XDWMC": organization,
        }
        for person, organization in (
            ("高天琦", "东北农业大学"),
            ("王渤洋", "南开大学"),
            ("钱婷婷", "上海应用技术大学"),
            ("史耀媛", "西安电子科技大学"),
        )
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "教育部人文社科", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "05040003", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    scope = next(
        item for item in context.role_scopes
        if item["role_type"] == "work_or_project"
    )
    displays = set(scope["submitted_identities"].values())
    assert scope["submitted_identity_count"] == 4
    assert len(displays) == 4
    assert all("生成式人工智能对大学生就业的影响及对策研究;" in item for item in displays)
    assert any("高天琦;东北农业大学" in item for item in displays)


def test_duplicate_patent_numbers_use_title_before_organization_discriminator() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = [
        "ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "XDWMC",
    ]
    rows = [
        {
            "ZYLBM": "06020007", "ZYLB": "中国专利奖", "LXNF": "2025",
            "XMBH": "ZL201910243534.X", "XMMC": title,
            "XMLB": "中国专利优秀奖项目", "XDWMC": organization,
        }
        for title, organization in (
            ("一种基于动态白盒的数据处理方法、装置及设备", "杭州海康威视数字技术股份有限公司"),
            ("一种空气交换量的测量方法及系统", "中国辐射防护研究院"),
        )
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "中国专利奖", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "06020007", "year": "2025"},
        imported_files=[imported], registry={table_code: spec},
    )

    scope = next(item for item in context.role_scopes if item["role_type"] == "work_or_project")
    displays = set(scope["submitted_identities"].values())
    assert displays == {
        "ZL201910243534.X;一种基于动态白盒的数据处理方法、装置及设备",
        "ZL201910243534.X;一种空气交换量的测量方法及系统",
    }


def test_art_project_categories_do_not_create_false_person_or_organization_awards() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = [
        "ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "XFZRXM",
        "XCYRXM", "XDWMC",
    ]
    rows = [
        {
            "ZYLBM": "05060001", "ZYLB": "国家艺术基金", "LXNF": "2025",
            "XMMC": "作品甲", "XMLB": "美术个人创作项目",
            "XFZRXM": "负责人甲", "XDWMC": "单位甲",
        },
        {
            "ZYLBM": "05060001", "ZYLB": "国家艺术基金", "LXNF": "2025",
            "XMMC": "作品乙", "XMLB": "美术组织创作项目",
            "XFZRXM": "负责人乙", "XDWMC": "单位乙",
        },
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "国家艺术基金", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "05060001", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    assert {scope["role_type"] for scope in context.role_scopes} == {
        "work_or_project"
    }
    assert context.row_conservation["assigned_rows"] == 2


def test_research_project_uses_note_category_when_primary_scope_is_blank() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = ["ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "BZ"]
    rows = [
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMBH": "P-1", "XMMC": "Planning", "XMLB": "Planning Fund",
        },
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMMC": "Theory", "BZ": "Theory Special Project",
        },
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMMC": "Counsellor", "BZ": "Counsellor Special Project",
        },
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "Research Award", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "05040003", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    scopes = context.role_scopes
    assert len(scopes) == 3
    assert {scope["business_scope"].get("BZ") for scope in scopes} == {
        None, "Theory Special Project", "Counsellor Special Project",
    }
    assert context.row_conservation["assigned_rows"] == 3


def test_research_project_uses_complete_note_category_with_subtype() -> None:
    table_code = "CON_GG_XK_KXYJ_KYXM"
    codes = ["ZYLBM", "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "BZ"]
    rows = [
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMBH": "P-1", "XMMC": "Planning A", "XMLB": "Planning Fund",
            "BZ": "General Project",
        },
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMBH": "P-2", "XMMC": "Planning B", "XMLB": "Planning Fund",
            "BZ": "Western Project",
        },
        {
            "ZYLBM": "05040003", "ZYLB": "Research Award", "LXNF": "2025",
            "XMBH": "P-3", "XMMC": "Youth", "XMLB": "Youth Fund",
            "BZ": "Western Project",
        },
    ]
    imported = _imported(table_code, codes, rows, year="2025")
    spec = build_template_spec(table_code, "Research Award", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "05040003", "year": "2025"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    scopes = context.role_scopes
    assert len(scopes) == 3
    assert {
        (scope["business_scope"].get("BZ"), scope["business_scope"].get("XMLB"))
        for scope in scopes
    } == {
        ("General Project", "Planning Fund"),
        ("Western Project", "Planning Fund"),
        ("Western Project", "Youth Fund"),
    }
    assert context.row_conservation["assigned_rows"] == 3
