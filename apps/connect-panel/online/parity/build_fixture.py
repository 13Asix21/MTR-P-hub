"""T01：确定性、无私密内容的在线引擎对拍 fixture 工厂。

只写 apps/connect-panel/online/parity/fixtures；只读引用主仓 stage2 fixture。
连续运行的文件树与 manifest 字节必须完全一致（mtime 不参与内容摘要）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PARITY_DIR = Path(__file__).resolve().parent
ONLINE_DIR = PARITY_DIR.parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = PARITY_DIR / "fixtures"
VAULT_ROOT = FIXTURE_ROOT / "vault"
STAGE2_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "stage2" / "vault"
MAX_FILE_BYTES = 1_048_576


def _file(relative: str, content: str | bytes, case_id: str, intent: str) -> dict[str, object]:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return {"path": "2-Permanent/" + relative, "bytes": raw, "case_id": case_id, "intent": intent}


def build_cases() -> list[dict[str, object]]:
    """34 类边界逐条登记；不以一个泛化样例冒充全部异常。"""
    cases = [
        _file("invalid/01-unclosed.md", "---\ntitle: no closing\nbody\n", "01", "首行 --- 无闭合，INVALID_YAML"),
        _file("invalid/02-syntax.md", "---\nkey: [1,2\n---\nbody\n", "02", "YAML 语法错，INVALID_YAML"),
        _file("invalid/03-sequence.md", "---\n- a\n- b\n---\nbody\n", "03", "frontmatter 序列而非 mapping"),
        _file("invalid/04-duplicate.md", "---\ntitle: a\ntitle: b\n---\nbody\n", "04", "重复键拒绝"),
        _file("invalid/05-tags-int.md", "---\ntags: 12\n---\nbody\n", "05", "tags int 非法"),
        _file("invalid/06-tags-mixed.md", "---\ntags: [a, 2]\n---\nbody\n", "06", "tags list 混入非 str"),
        _file("invalid/07-tags-bool.md", "---\ntags: true\n---\nbody\n", "07", "tags bool 非法"),
        _file("invalid/07b-tags-null.md", "---\ntags: null\n---\nbody\n", "07-null", "tags null 非法"),
        _file("invalid/07c-tags-object.md", "---\ntags: {a: b}\n---\nbody\n", "07-object", "tags mapping 非法"),
        _file("invalid/07d-tags-nested.md", "---\ntags: [[a]]\n---\nbody\n", "07-nested", "tags 嵌套数组非法"),
        _file("invalid/07e-multidoc.md", "---\ntitle: a\n...\ntitle: b\n---\nbody\n", "07-multidoc", "frontmatter 多文档非法"),
        _file("invalid/07f-unclosed-quote.md", "---\ntitle: 'oops\n---\nbody\n", "07-quote", "YAML 未闭合引号非法"),
        _file("encoding/08-bom-crlf.md", b"\xef\xbb\xbf---\r\ntitle: BOM CRLF\r\ntags: [mtr/trial]\r\n---\r\nBody [[target]]\r\n", "08", "BOM + CRLF 原文 hash 与解析"),
        _file("encoding/09-bom-no-frontmatter.md", b"\xef\xbb\xbf#mtr/rarely\r\nPlain body\r\n", "09", "BOM 无 frontmatter"),
        _file("encoding/10-mixed-crlf.md", b"first\r\nsecond\nthird\r\n[[target]]\r\n", "10", "混合 CRLF/LF 不改写原始字节"),
        _file("encoding/11-latin1.md", b"---\ntitle: bad\n---\n\xff\xfe\x80", "11", "非法 UTF-8，INVALID_ENCODING"),
        _file("encoding/12-empty.md", b"", "12", "空文件可解析、无标签"),
        _file("valid/13-no-frontmatter.md", "正文 #mtr/trial\n[[target]]\n", "13", "无 frontmatter 纯正文"),
        _file("valid/14-tags-string.md", "---\ntags: 'a b #mtr/rarely'\n---\nbody\n", "14", "tags 字符串空格拆分并 lstrip #"),
        _file("valid/15-tags-list.md", "---\ntags: ['#alpha', mtr/trial, beta]\n---\nbody\n", "15", "tags list 全 str"),
        _file("valid/16-empty-mapping.md", "---\n---\nbody\n", "16", "空 frontmatter mapping"),
        _file("valid/17-title-strip.md", "---\ntitle: '   trimmed title  '\n---\nbody\n", "17", "title strip"),
        _file("valid/18-body-blank-line.md", "---\ntitle: blank\n---\n\nbody after blank\n", "18", "frontmatter 后空行保留"),
        _file("tags/19-fences.md", "```md\n#mtr/trial\n[[target]]\n```\n~~~text\n#mtr/rarely\n~~~~\nvisible #mtr/trial\n", "19", "两种代码围栏内假标签/链接排除"),
        _file("tags/20-inline-code.md", "`#mtr/trial` and ``#mtr/rarely``\nvisible #mtr/trial\n", "20", "行内代码排除"),
        _file("tags/21-indented.md", "    #mtr/trial\n\t#mtr/rarely\n#mtr/rarely\n", "21", "四空格与 tab 缩进排除"),
        _file("tags/22-conflict.md", "front #mtr/trial\nback #mtr/rarely\n", "22", "trial+rarely conflict 优先级"),
        _file("tags/23-negative.md", "#mtr/trialx #mtr/rarely-extra x#mtr/trial\n#mtr/trial\n", "23", "标签后缀与前置负边界"),
        _file("links/24-wikilinks.md", "[[target|display]] [[#heading]] [[中文路径]]\n```\n[[target]]\n```\n`[[target]]`\n    [[target]]\n", "24", "wiki 别名、空目标、中文与代码排除"),
        _file("links/target.md", "target body\n", "24-target", "唯一 basename 反链目标"),
        _file("links/中文路径.md", "中文 target\n", "24-target-zh", "中文反链目标"),
        _file("filter/25-NOTE.MD", "#mtr/trial\n", "25", "大写 .MD 后缀入选"),
        _file("filter/30-a/b/c/deep.md", "---\ntitle: deep\n---\n[[target]]\n", "30", "三层嵌套目录"),
        _file("filter/28-one-mib.md", b"x" * MAX_FILE_BYTES, "28", "1 MiB 上边界入选并计算 hash"),
        _file("filter/29-too-large.md", b"y" * (MAX_FILE_BYTES + 1), "29", "1 MiB+1 FILE_TOO_LARGE 且不读正文"),
        _file("unicode/31-中文目录/中文 笔记.md", "---\ntitle: 中文\n---\n正文\n", "31", "NFC 中文与空格路径"),
        _file("unicode/32-na\u0308ive.md", "NFD file name\n", "32", "NFD 路径 NFC 键"),
        _file("unicode/33-全角／space note.md", "full width and spaces\n", "33", "全角与空格混排"),
        _file("unicode/34-caf\u00e9.md", "NFC variant\n", "34", "NFC/NFD 同键警告之 NFC 项"),
        _file("unicode/34-cafe\u0301.md", "NFD variant\n", "34", "NFC/NFD 同键警告之 NFD 项"),
        _file("unicode/casefold-strasse-\u00df.md", "casefold sharp s\n", "casefold-1", "Python casefold \u00df→ss"),
        _file("unicode/casefold-dotted-\u0130.md", "casefold dotted I\n", "casefold-2", "Python casefold \u0130→i+combining dot"),
        _file("yaml/scalars.md", "---\ntitle: scalar\nleading_zero: 01\nunderscored: 1_000\nscientific: 1e3\nratio: 1.20\nnull_value: null\nbool_value: true\narray_value: [a, 2]\nobject_value: {x: 1}\n---\nbody\n", "detail-scalars", "frontmatter 值类型与 JSON 展示"),
    ]
    cases.extend([
        _file("Resources/MTR-P-System/x.md", "#mtr/trial excluded\n", "27", "excluded root 一不入选"),
        _file("Areas/AI-Assets/_templates/y.md", "#mtr/rarely excluded\n", "27", "excluded root 二不入选"),
        _file("Resources/MTR-P-System2/kept.md", "prefix sibling kept\n", "27-boundary", "完整路径段排除，A/B2 不误排"),
    ])
    return cases


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def stage2_manifest() -> dict[str, object]:
    files = [
        {
            "path": path.relative_to(STAGE2_ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
            "managed": not path.relative_to(STAGE2_ROOT).as_posix().startswith("2-Permanent/Resources/MTR-P-System/"),
        }
        for path in sorted(STAGE2_ROOT.rglob("*"), key=lambda item: item.as_posix()) if path.is_file()
    ]
    return {
        "schema_version": "1",
        "source": "tests/fixtures/stage2/vault",
        "access": "read-only reference; files are not copied or modified",
        "files": files,
    }


def main() -> int:
    if not STAGE2_ROOT.is_dir():
        raise SystemExit(f"stage2 fixture 不存在：{STAGE2_ROOT}")
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    expected_paths = {str(case["path"]) for case in cases}
    expected_paths.update({"2-Permanent/filter/26-ignore.txt", "outside-root.md"})
    unexpected = [path for path in VAULT_ROOT.rglob("*") if path.is_file() and path.relative_to(VAULT_ROOT).as_posix() not in expected_paths]
    if unexpected:
        raise SystemExit("fixture 目录含未登记文件，拒绝静默删除：" + ", ".join(str(path) for path in unexpected))
    manifest_files = []
    for case in cases:
        relative = str(case["path"])
        target = VAULT_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = case["bytes"]
        assert isinstance(raw, bytes)
        target.write_bytes(raw)
        manifest_files.append({
            "path": relative,
            "case_id": case["case_id"],
            "intent": case["intent"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "managed": not relative.startswith(("2-Permanent/Resources/MTR-P-System/", "2-Permanent/Areas/AI-Assets/_templates/")),
        })
    (VAULT_ROOT / "2-Permanent/filter/26-ignore.txt").write_bytes(b"not markdown #mtr/trial\n")
    (VAULT_ROOT / "outside-root.md").write_bytes(b"outside managed root\n")
    managed_count = sum(bool(item["managed"]) and str(item["path"]).lower().endswith(".md") for item in manifest_files)
    manifest = {
        "schema_version": "1",
        "generator": "online/parity/build_fixture.py",
        "privacy": "synthetic public test content only",
        "expected_managed_markdown": managed_count,
        "expected_stage2_managed_markdown": 6,
        "expected_combined_fixture_count": managed_count + 6,
        "fixture_tree_sha256": tree_digest(VAULT_ROOT),
        "files": manifest_files,
    }
    (FIXTURE_ROOT / "fixtures_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (FIXTURE_ROOT / "stage2_manifest.json").write_text(json.dumps(stage2_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"managed_markdown": managed_count, "combined": managed_count + 6, "tree_sha256": manifest["fixture_tree_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
