"""
CATIA V5 TLB Extractor
======================
CATIA の .tlb ファイルを読み込み、型情報を JSON として出力する。

使い方（UV 仮想環境）:
    uv run python extract.py              # デフォルトパスで CATIA を検索
    uv run python extract.py --out catia_types.json
    uv run python extract.py --tlb "C:/path/to/CATIA.tlb"
    uv run python extract.py --list       # 検出された TLB 一覧を表示

出力 JSON スキーマ:
    {
      "TypeName": {
        "kind": "class" | "interface" | "enum" | "module",
        "properties": [{ "name": "...", "returnType": "..." }],
        "methods": [{ "name": "...", "returnType": "...", "params": [{ "name": "...", "type": "..." }] }]
      }
    }
"""

import argparse
import json
import sys
import os
import glob
from pathlib import Path

try:
    import comtypes
    import comtypes.typeinfo
    import comtypes.automation
    from comtypes.typeinfo import (
        TYPEKIND,
        TKIND_COCLASS,
        TKIND_INTERFACE,
        TKIND_DISPATCH,
        TKIND_ENUM,
        TKIND_MODULE,
        TKIND_RECORD,
    )
    from comtypes.automation import (
        VT_VOID,
        VT_BOOL,
        VT_I2,
        VT_I4,
        VT_R4,
        VT_R8,
        VT_BSTR,
        VT_DISPATCH,
        VT_UNKNOWN,
        VT_VARIANT,
        VT_PTR,
        VT_USERDEFINED,
        VT_EMPTY,
        VT_INT,
        VT_UINT,
        VT_I8,
        VT_UI2,
        VT_UI4,
    )
    from comtypes import GUID
except ImportError:
    print("ERROR: comtypes がインストールされていません。", file=sys.stderr)
    print("  pip install comtypes", file=sys.stderr)
    sys.exit(1)


# CATIA V5 の標準インストールパス候補
CATIA_TLB_SEARCH_PATHS = [
    r"C:\Program Files\Dassault Systemes\B*\intel_a\code\bin\*.tlb",
    r"C:\Program Files (x86)\Dassault Systemes\B*\intel_a\code\bin\*.tlb",
]

# 抽出対象ライブラリ名（部分一致）
TARGET_LIBS = [
    "CATIA",
    "ProductStructure",
    "MechanicalModeler",
    "PartInterfaces",
    "SketcherInterfaces",
    "WireFrame",
    "GSMInterfaces",
]


def vt_to_str(vt: int, typelib=None, tinfo=None) -> str:
    """VARIANT TYPE を文字列に変換する。"""
    mapping = {
        VT_VOID: "void",
        VT_BOOL: "Boolean",
        VT_I2: "Integer",
        VT_I4: "Long",
        VT_INT: "Long",
        VT_UINT: "Long",
        VT_UI2: "Integer",
        VT_UI4: "Long",
        VT_I8: "LongLong",
        VT_R4: "Single",
        VT_R8: "Double",
        VT_BSTR: "String",
        VT_DISPATCH: "Object",
        VT_UNKNOWN: "Object",
        VT_VARIANT: "Variant",
        VT_EMPTY: "void",
    }
    # ポインタ修飾を外す
    pure_vt = vt & ~0x4000  # VT_BYREF
    if pure_vt == VT_PTR:
        return "Object"
    if pure_vt == VT_USERDEFINED and tinfo is not None:
        try:
            ref = tinfo.GetRefTypeOfImplType(-1) if False else None
            href = tinfo.GetTypeAttr().tdescUnion.hreftype if False else None
            # USERDEFINED の解決
            ta = tinfo.GetTypeAttr()
            # ユーザー定義型は tinfo の名前から取得する
            name, _, _, _ = tinfo.GetDocumentation(-1)
            return name
        except Exception:
            return "Variant"
    return mapping.get(pure_vt, "Variant")


def resolve_type(tdesc, tinfo, typelib) -> str:
    """TYPEDESC を再帰的に解決して型名文字列を返す。"""
    vt = tdesc.vt
    if vt == VT_PTR:
        inner = tdesc._.lptdesc.contents
        return resolve_type(inner, tinfo, typelib)
    if vt == VT_USERDEFINED:
        href = tdesc._.hreftype
        try:
            ref_tinfo = tinfo.GetRefTypeInfo(href)
            name, _, _, _ = ref_tinfo.GetDocumentation(-1)
            return name
        except Exception:
            return "Variant"
    return vt_to_str(vt)


def extract_typelib(typelib) -> dict:
    """1 つの ITypeLib から型情報を抽出して dict を返す。"""
    result = {}
    count = typelib.GetTypeInfoCount()
    for i in range(count):
        try:
            tinfo = typelib.GetTypeInfo(i)
            ta = tinfo.GetTypeAttr()
            name, _, _, _ = tinfo.GetDocumentation(-1)

            kind_map = {
                TKIND_COCLASS: "class",
                TKIND_INTERFACE: "interface",
                TKIND_DISPATCH: "class",
                TKIND_ENUM: "enum",
                TKIND_MODULE: "module",
                TKIND_RECORD: "record",
            }
            kind = kind_map.get(ta.typekind, "interface")

            entry: dict = {"kind": kind, "properties": [], "methods": []}

            if ta.typekind == TKIND_ENUM:
                # enum メンバーを取得
                for v in range(ta.cVars):
                    try:
                        vd = tinfo.GetVarDesc(v)
                        vname, _, _, _ = tinfo.GetDocumentation(vd.memid)
                        val = vd._.lpvarValue.contents._.lVal if vd.varkind == 2 else v
                        entry["members"] = entry.get("members", [])
                        entry["members"].append({"name": vname, "value": val})
                    except Exception:
                        pass
            else:
                # プロパティ・メソッドを取得
                for f in range(ta.cFuncs):
                    try:
                        fd = tinfo.GetFuncDesc(f)
                        fname, _, _, _ = tinfo.GetDocumentation(fd.memid)

                        # 戻り値の型
                        ret_type = resolve_type(fd.elemdescFunc.tdesc, tinfo, typelib)

                        # パラメータ
                        params = []
                        for p in range(fd.cParams):
                            ep = fd.lprgelemdescParam[p]
                            ptype = resolve_type(ep.tdesc, tinfo, typelib)
                            try:
                                pnames = tinfo.GetNames(fd.memid, fd.cParams + 1)
                                pname = pnames[p + 1] if p + 1 < len(pnames) else f"p{p}"
                            except Exception:
                                pname = f"p{p}"
                            params.append({"name": pname, "type": ptype})

                        # INVOKE_PROPERTYGET / PROPERTYPUT → property
                        INVOKE_PROPERTYGET = 2
                        INVOKE_PROPERTYPUT = 4
                        if fd.invkind in (INVOKE_PROPERTYGET, INVOKE_PROPERTYPUT):
                            if fd.invkind == INVOKE_PROPERTYGET:
                                entry["properties"].append({
                                    "name": fname,
                                    "returnType": ret_type,
                                })
                        else:
                            entry["methods"].append({
                                "name": fname,
                                "returnType": ret_type,
                                "params": params,
                            })
                    except Exception:
                        pass

            # 重複プロパティを排除
            seen_props = set()
            dedup_props = []
            for p in entry["properties"]:
                if p["name"] not in seen_props:
                    seen_props.add(p["name"])
                    dedup_props.append(p)
            entry["properties"] = dedup_props

            result[name] = entry
        except Exception as e:
            pass

    return result


def find_catia_tlbs() -> list[str]:
    """CATIA の TLB ファイルを自動検索する。"""
    found = []
    for pattern in CATIA_TLB_SEARCH_PATHS:
        for path in glob.glob(pattern, recursive=False):
            fname = os.path.basename(path)
            base = os.path.splitext(fname)[0]
            if any(t.lower() in base.lower() for t in TARGET_LIBS):
                found.append(path)
    return sorted(set(found))


def load_typelib(path: str):
    """指定パスの TLB を読み込む。"""
    try:
        return comtypes.typeinfo.LoadTypeLibEx(path)
    except Exception as e:
        print(f"  WARN: {path} の読み込みに失敗: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="CATIA V5 TLB → JSON 変換ツール")
    parser.add_argument("--out", default="catia_types.json", help="出力JSONファイルパス")
    parser.add_argument("--tlb", nargs="*", help="TLBファイルパス（複数指定可）")
    parser.add_argument("--list", action="store_true", help="見つかったTLBを一覧表示して終了")
    args = parser.parse_args()

    if args.tlb:
        tlb_paths = args.tlb
    else:
        print("CATIA TLB ファイルを検索中...", flush=True)
        tlb_paths = find_catia_tlbs()
        if not tlb_paths:
            print("ERROR: CATIA TLB ファイルが見つかりませんでした。", file=sys.stderr)
            print("  --tlb オプションで明示的にパスを指定してください。", file=sys.stderr)
            print("  例: python extract.py --tlb \"C:/CATIA/CATIA.tlb\"", file=sys.stderr)
            sys.exit(1)

    if args.list:
        for p in tlb_paths:
            print(p)
        return

    print(f"対象 TLB ファイル ({len(tlb_paths)} 件):")
    for p in tlb_paths:
        print(f"  {p}")

    all_types: dict = {}
    for path in tlb_paths:
        print(f"\n読み込み中: {os.path.basename(path)} ...", flush=True)
        tl = load_typelib(path)
        if tl is None:
            continue
        types = extract_typelib(tl)
        print(f"  → {len(types)} 型を抽出")
        # 後から読んだものが優先（重複時は上書きしない）
        for k, v in types.items():
            if k not in all_types:
                all_types[k] = v

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_types, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {len(all_types)} 型を {out_path} に出力しました。")


if __name__ == "__main__":
    main()
