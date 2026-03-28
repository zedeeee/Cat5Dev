"""
CATIA V5 TLB → SQLite 変換ツール
===================================
指定ディレクトリ内のすべての .tlb ファイルを読み込み、
VBA Language Server 用の SQLite データベースを生成する。

スキーマ:
    interfaces(id, name, parent_id)
    methods(id, interface_id, name, return_type)
    properties(id, interface_id, name, type)
    parameters(id, method_id, name, type, position)

使い方（UV 仮想環境）:
    # ディレクトリ内の全 TLB を処理（推奨）
    uv run python extract.py "C:/Program Files/Dassault Systemes/B32/win_b64/code/bin"

    # 出力先を明示する場合
    uv run python extract.py --out catia_api.db "C:/Program Files/Dassault Systemes/B32/win_b64/code/bin"

    # 特定ファイルのみ
    uv run python extract.py --out catia_api.db "C:/...bin/CATPartTypeLib.tlb"

    # 検出された TLB 一覧を確認してから実行する場合
    uv run python extract.py --list "C:/Program Files/Dassault Systemes/B32/win_b64/code/bin"
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# comtypes 依存のインポート
# ---------------------------------------------------------------------------
try:
    import comtypes.typeinfo
    import comtypes.automation
    from comtypes.typeinfo import (
        TKIND_COCLASS,
        TKIND_INTERFACE,
        TKIND_DISPATCH,
    )
    from comtypes.automation import (
        VT_VOID, VT_BOOL, VT_I2, VT_I4, VT_R4, VT_R8,
        VT_BSTR, VT_DISPATCH, VT_UNKNOWN, VT_VARIANT,
        VT_PTR, VT_USERDEFINED, VT_EMPTY,
        VT_INT, VT_UINT, VT_I8, VT_UI2, VT_UI4,
    )
    # REGKIND 定数（comtypes のバージョンによって場所が異なる）
    try:
        from comtypes.typeinfo import REGKIND_NONE
    except ImportError:
        REGKIND_NONE = 2  # REGKIND_NONE の数値

except ImportError as e:
    print(f"ERROR: comtypes がインストールされていません: {e}", file=sys.stderr)
    print("  uv run python extract.py を使用してください", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# VARIANT TYPE → 文字列変換
# ---------------------------------------------------------------------------
_VT_MAP = {
    VT_VOID: "void", VT_BOOL: "Boolean",
    VT_I2: "Integer", VT_I4: "Long", VT_INT: "Long", VT_UINT: "Long",
    VT_UI2: "Integer", VT_UI4: "Long", VT_I8: "LongLong",
    VT_R4: "Single", VT_R8: "Double",
    VT_BSTR: "String", VT_DISPATCH: "Object",
    VT_UNKNOWN: "Object", VT_VARIANT: "Variant", VT_EMPTY: "void",
}
_VT_BYREF = 0x4000
_VT_ARRAY = 0x2000


def _resolve_type(tdesc, tinfo, tlb) -> str:
    """TYPEDESC を再帰的に解決して型名文字列を返す。"""
    vt = tdesc.vt & ~_VT_BYREF & ~_VT_ARRAY
    if vt == VT_PTR:
        try:
            return _resolve_type(tdesc._.lptdesc.contents, tinfo, tlb)
        except Exception:
            return "Object"
    if vt == VT_USERDEFINED:
        try:
            href = tdesc._.hreftype
            ref_ti = tinfo.GetRefTypeInfo(href)
            name, *_ = ref_ti.GetDocumentation(-1)
            return str(name) if name else "Variant"
        except Exception:
            return "Variant"
    return _VT_MAP.get(vt, "Variant")


def _get_doc(tinfo, memid: int):
    """(name, doc) を返す。取得失敗時は ("", "")。"""
    try:
        result = tinfo.GetDocumentation(memid)
        if isinstance(result, tuple):
            name = str(result[0]) if result[0] is not None else ""
            doc = str(result[1]) if len(result) > 1 and result[1] is not None else ""
            return name, doc
        return str(result), ""
    except Exception:
        return "", ""


def _get_parent_name(tinfo, tattr) -> Optional[str]:
    """継承元インターフェース名を返す。なければ None。"""
    n_impl = int(getattr(tattr, "cImplTypes", 0))
    for i in range(n_impl):
        try:
            href = tinfo.GetRefTypeOfImplType(i)
            parent_ti = tinfo.GetRefTypeInfo(href)
            parent_name, _ = _get_doc(parent_ti, -1)
            # IDispatch / IUnknown は親として不要
            if parent_name and parent_name not in ("IDispatch", "IUnknown", "_IDispatch"):
                return parent_name
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# TLB ロード
# ---------------------------------------------------------------------------
def _load_tlb(path: str):
    """TLB を読み込む。失敗時は None を返す。"""
    # まず REGKIND_NONE で試みる（DLL依存の解決エラーを回避）
    for regkind in (REGKIND_NONE, 0):
        try:
            return comtypes.typeinfo.LoadTypeLibEx(path, regkind)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# TLB → DB 書き込み
# ---------------------------------------------------------------------------
def _process_tlb(path: str, db: "TLBDatabase") -> int:
    """1 つの TLB を処理して DB に書き込む。追加した型数を返す。"""
    tlb = _load_tlb(path)
    if tlb is None:
        return 0

    count = 0
    n = tlb.GetTypeInfoCount()
    for i in range(n):
        try:
            tinfo = tlb.GetTypeInfo(i)
            tattr = tinfo.GetTypeAttr()
            kind = int(getattr(tattr, "typekind", -1))

            if kind not in (TKIND_INTERFACE, TKIND_DISPATCH, TKIND_COCLASS):
                continue

            name, _ = _get_doc(tinfo, -1)
            if not name:
                continue

            parent_name = _get_parent_name(tinfo, tattr)
            iface_id = db.ensure_interface(name, parent_name)

            # メソッド・プロパティ
            n_func = int(getattr(tattr, "cFuncs", 0))
            for f in range(n_func):
                try:
                    fd = tinfo.GetFuncDesc(f)
                    fname, _ = _get_doc(tinfo, fd.memid)
                    if not fname:
                        continue

                    ret_type = _resolve_type(fd.elemdescFunc.tdesc, tinfo, tlb)

                    # パラメータ名を取得
                    try:
                        all_names = tinfo.GetNames(fd.memid, fd.cParams + 1)
                        param_names = list(all_names[1:]) if len(all_names) > 1 else []
                    except Exception:
                        param_names = []

                    INVOKE_PROPERTYGET = 2
                    INVOKE_PROPERTYPUT = 4
                    INVOKE_PROPERTYPUTREF = 8

                    if fd.invkind & (INVOKE_PROPERTYGET | INVOKE_PROPERTYPUT | INVOKE_PROPERTYPUTREF):
                        if fd.invkind == INVOKE_PROPERTYGET:
                            db.add_property(iface_id, fname, ret_type)
                    else:
                        method_id = db.add_method(iface_id, fname, ret_type)
                        for pos, ep in enumerate(fd.lprgelemdescParam[:fd.cParams]):
                            ptype = _resolve_type(ep.tdesc, tinfo, tlb)
                            pname = param_names[pos] if pos < len(param_names) else f"p{pos}"
                            db.add_param(method_id, pname, ptype, pos)

                    count += 1
                except Exception:
                    pass
        except Exception:
            pass

    return count


# ---------------------------------------------------------------------------
# SQLite データベースラッパー
# ---------------------------------------------------------------------------
class TLBDatabase:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self._create_schema()
        self._iface_cache: dict[str, int] = {}

    def _create_schema(self):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS interfaces (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL UNIQUE,
                parent_id INTEGER REFERENCES interfaces(id)
            );
            CREATE TABLE IF NOT EXISTS methods (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                interface_id INTEGER NOT NULL REFERENCES interfaces(id),
                name         TEXT    NOT NULL,
                return_type  TEXT    NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS properties (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                interface_id INTEGER NOT NULL REFERENCES interfaces(id),
                name         TEXT    NOT NULL,
                type         TEXT    NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS parameters (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                method_id INTEGER NOT NULL REFERENCES methods(id),
                name      TEXT    NOT NULL,
                type      TEXT    NOT NULL DEFAULT '',
                position  INTEGER NOT NULL DEFAULT 0
            );
        """)
        c.commit()

    def ensure_interface(self, name: str, parent_name: Optional[str]) -> int:
        """インターフェースを登録し ID を返す。既存の場合はその ID を返す。"""
        if name in self._iface_cache:
            return self._iface_cache[name]

        parent_id = None
        if parent_name:
            parent_id = self._iface_cache.get(parent_name)
            if parent_id is None:
                # 親が未登録の場合、先に登録（parent なしで）
                self.conn.execute(
                    "INSERT OR IGNORE INTO interfaces(name) VALUES(?)", (parent_name,)
                )
                row = self.conn.execute(
                    "SELECT id FROM interfaces WHERE name=?", (parent_name,)
                ).fetchone()
                parent_id = row[0] if row else None
                if parent_id:
                    self._iface_cache[parent_name] = parent_id

        self.conn.execute(
            "INSERT OR IGNORE INTO interfaces(name, parent_id) VALUES(?,?)",
            (name, parent_id),
        )
        if parent_id:
            # 既存レコードの parent_id も更新（先行登録されていた場合）
            self.conn.execute(
                "UPDATE interfaces SET parent_id=? WHERE name=? AND parent_id IS NULL",
                (parent_id, name),
            )
        row = self.conn.execute(
            "SELECT id FROM interfaces WHERE name=?", (name,)
        ).fetchone()
        iid = row[0]
        self._iface_cache[name] = iid
        return iid

    def add_method(self, iface_id: int, name: str, return_type: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO methods(interface_id, name, return_type) VALUES(?,?,?)",
            (iface_id, name, return_type),
        )
        return cur.lastrowid

    def add_property(self, iface_id: int, name: str, prop_type: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO properties(interface_id, name, type) VALUES(?,?,?)",
            (iface_id, name, prop_type),
        )

    def add_param(self, method_id: int, name: str, param_type: str, position: int):
        self.conn.execute(
            "INSERT INTO parameters(method_id, name, type, position) VALUES(?,?,?,?)",
            (method_id, name, param_type, position),
        )

    def create_indexes(self):
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_iface_name    ON interfaces(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_method_iface  ON methods(interface_id);
            CREATE INDEX IF NOT EXISTS idx_method_name   ON methods(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_prop_iface    ON properties(interface_id);
            CREATE INDEX IF NOT EXISTS idx_prop_name     ON properties(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_param_method  ON parameters(method_id);
        """)
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def stats(self) -> dict:
        c = self.conn
        return {
            "interfaces": c.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0],
            "methods":    c.execute("SELECT COUNT(*) FROM methods").fetchone()[0],
            "properties": c.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
        }


# ---------------------------------------------------------------------------
# TLB ファイル検索
# ---------------------------------------------------------------------------
CATIA_TLB_DEFAULT_PATTERNS = [
    r"C:\Program Files\Dassault Systemes\B*\win_b64\code\bin\*.tlb",
    r"C:\Program Files\Dassault Systemes\B*\intel_a\code\bin\*.tlb",
    r"C:\Program Files (x86)\Dassault Systemes\B*\win_b64\code\bin\*.tlb",
]


def find_tlb_files(paths: list[str], recursive: bool = False) -> list[str]:
    """引数リストから TLB ファイルのパス一覧を返す。"""
    result = []
    seen: set[str] = set()

    def add(p: str):
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            result.append(p)

    for raw in paths:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
        if os.path.isdir(p):
            pattern = "**/*.tlb" if recursive else "*.tlb"
            for f in glob.glob(os.path.join(p, pattern), recursive=recursive):
                add(f)
        elif "*" in raw or "?" in raw:
            for f in glob.glob(raw, recursive=recursive):
                add(f)
        else:
            add(p)

    return sorted(result)


def find_catia_tlbs_auto() -> list[str]:
    """CATIA の標準インストールパスから TLB を自動検索する。"""
    result = []
    seen: set[str] = set()
    for pat in CATIA_TLB_DEFAULT_PATTERNS:
        for f in glob.glob(pat, recursive=False):
            a = os.path.abspath(f)
            if a not in seen:
                seen.add(a)
                result.append(a)
    return sorted(result)


# ---------------------------------------------------------------------------
# メインエントリ
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="CATIA V5 TLB → SQLite 変換ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "paths",
        nargs="*",
        help="TLB ファイルまたはディレクトリ（省略時は自動検索）",
    )
    ap.add_argument("--out", default="catia_api.db", help="出力 SQLite ファイルパス")
    ap.add_argument("--recursive", "-r", action="store_true", help="ディレクトリを再帰検索")
    ap.add_argument("--list", action="store_true", help="処理対象 TLB を一覧表示して終了")
    ap.add_argument("--batch", type=int, default=50, help="コミット間隔（デフォルト: 50 TLB）")
    args = ap.parse_args()

    if args.paths:
        tlb_files = find_tlb_files(args.paths, recursive=args.recursive)
    else:
        print("CATIA TLB ファイルを自動検索中...", flush=True)
        tlb_files = find_catia_tlbs_auto()

    if not tlb_files:
        print("ERROR: TLB ファイルが見つかりませんでした。", file=sys.stderr)
        print("  パスを引数で指定してください:", file=sys.stderr)
        print('  uv run python extract.py "C:/Program Files/Dassault Systemes/B32/win_b64/code/bin"', file=sys.stderr)
        sys.exit(1)

    print(f"対象 TLB ファイル: {len(tlb_files)} 件", flush=True)
    if args.list:
        for f in tlb_files:
            print(f"  {f}")
        return

    # 既存 DB を削除して新規作成
    if os.path.exists(args.out):
        os.remove(args.out)

    db = TLBDatabase(args.out)
    total_types = 0
    ok_count = 0
    fail_count = 0

    for idx, path in enumerate(tlb_files, 1):
        fname = os.path.basename(path)
        n = _process_tlb(path, db)
        if n > 0:
            ok_count += 1
            total_types += n
            print(f"  [{idx:3d}/{len(tlb_files)}] {fname}: {n} メンバー", flush=True)
        else:
            fail_count += 1
            print(f"  [{idx:3d}/{len(tlb_files)}] {fname}: スキップ（読み込み失敗）", flush=True)

        if idx % args.batch == 0:
            db.commit()

    db.commit()
    print("\nインデックス作成中...", flush=True)
    db.create_indexes()

    stats = db.stats()
    db_size = os.path.getsize(args.out)
    db.close()

    print(f"\n完了:")
    print(f"  TLB: {ok_count} 成功 / {fail_count} スキップ")
    print(f"  インターフェース: {stats['interfaces']:,}")
    print(f"  メソッド:         {stats['methods']:,}")
    print(f"  プロパティ:       {stats['properties']:,}")
    print(f"  DB サイズ:        {db_size / 1024 / 1024:.1f} MB")
    print(f"  出力先:           {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
