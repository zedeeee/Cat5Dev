# Cat5Dev LSP 実装計画

## 背景・目的

Cat5Dev は CATIA V5 VBA マクロ開発用の VSCode 拡張。現在は簡易フォーマッタと Lint を Go 製バックエンド（`vbafmt.exe`）で実装済み。

**目標:** CATIA V5 COM オブジェクトのインテリセンス（型認識による補完・診断・ホバー）を LSP として実装する。

### 調査結果サマリー

| 選択肢 | 結論 |
|--------|------|
| tree-sitter VBA grammar | 未成熟。`With` / `Select Case` / `On Error` 等が未実装。使用不可。 |
| SSlinky/VBA-LanguageServer | 補完は未実装だが、MS-VBAL 仕様準拠の `vba.g4`（55KB）が MIT で流用可能。 |
| ANTLR4 + Go ランタイム | `vba.g4` を利用して Go 製パーサーを生成できる。現実的な選択肢。 |

---

## アーキテクチャ

```
VSCode (TypeScript クライアント)
    ↕ LSP over stdio
vba-ls.exe (Go 製 Language Server)
    ├── VBA パーサー（ANTLR4 Go ランタイム + vba.g4）
    │     └── シンボルテーブル（スコープ付き Dim 宣言解析）
    ├── TLB データベース（JSON → 起動時メモリ展開）
    └── LSP プロトコル実装（補完・診断・ホバー）
```

- 現状の `vbafmt.exe`（フォーマッタ・Lint）とは **別バイナリ** として実装
- 将来的に統合を検討

---

## フェーズ構成

### Phase 0: TLB データベース構築

**場所:** `tools/tlb-extractor/`（Python スクリプト）

Python + `comtypes` / `win32com` で CATIA の `.tlb` を読み込み、JSON として出力する。
一度生成したデータベースは `vba-ls.exe` に同梱し、起動時にメモリ展開して使用する。

**出力 JSON 形式:**
```json
{
  "PartDocument": {
    "kind": "class",
    "properties": [
      { "name": "Part", "returnType": "Part" },
      { "name": "Product", "returnType": "Product" }
    ],
    "methods": [
      { "name": "Close", "returnType": "void", "params": [] }
    ]
  }
}
```

**優先対象ライブラリ（例）:**
- `CATIA.tlb`
- `ProductStructure.tlb`
- `MechanicalModeler.tlb`
- `PartInterfaces.tlb`

---

### Phase 1: VBA パーサー基盤（Go + ANTLR4）

**場所:** `tools/vba-ls/`（新規）

1. ANTLR4 Go ランタイム導入
   - `github.com/antlr4-go/antlr/v4`
2. SSlinky の `vba.g4`（MIT）を `tools/vba-ls/antlr/` に配置・必要に応じて調整
3. Go パーサーコードを生成（`antlr4` CLI で生成）
4. リスナーパターンで **シンボルテーブル** を構築

**シンボルテーブルの構造（例）:**
```go
type Symbol struct {
    Name     string
    TypeName string // "PartDocument" 等
    Scope    string // "module" / "Sub:FooSub" 等
    Line     int
}
```

**解析対象:**
- `Dim x As TypeName`（ローカル変数・モジュールレベル変数）
- `Public / Private` 宣言
- Sub / Function のスコープ境界

---

### Phase 2: LSP サーバー実装（Go）

**場所:** `tools/vba-ls/server.go`

**LSP フレームワーク:** `github.com/tliron/glsp`（Go 製 LSP 実装ライブラリ）

**実装機能:**

| 機能 | LSP メソッド | 内容 |
|------|-------------|------|
| メンバー補完 | `textDocument/completion` | `obj.` 入力時に TLB データベースからメンバー一覧を返す |
| ホバー情報 | `textDocument/hover` | メソッド・プロパティのシグネチャと説明を表示 |
| 診断 | `textDocument/publishDiagnostics` | 存在しないメンバーへのアクセスをエラーとして表示 |

**補完フロー（例）:**
```
1. カーソル位置の前テキストから `oDoc.` を検出
2. シンボルテーブルで `oDoc` の型名を解決 → "PartDocument"
3. TLB データベースから "PartDocument" のメンバー一覧を取得
4. CompletionItem の配列として返す
```

**スコープ対象外（初期実装では対応しない）:**
- `Set oDoc = CATIA.ActiveDocument` 等の右辺からの型推論
- `Variant` 型の動的解決

---

### Phase 3: VSCode クライアント統合

**変更ファイル:** `src/extension.ts`、`src/lspClient.ts`（新規）

- `vscode-languageclient` ライブラリで `vba-ls.exe` を stdio LSP として起動
- 対象ファイル: `*.bas_utf` / `*.cls_utf` / `*.frm_utf`
- 既存の `vbafmt.exe`（フォーマッタ・Lint）と **並行動作**

---

### Phase 4: フォーマッタ改善（AST ベース化）

Phase 1 で構築したパーサーを `vbafmt` にも適用し、現状の行ベース処理を AST ベースに置き換える。

**改善される主なケース:**

| ケース | 現状（行ベース） | AST ベース後 |
|--------|----------------|-------------|
| `With...End With` のインデント | 脆弱 | 正確 |
| `Select Case` のインデント | 脆弱 | 正確 |
| コメント内のキーワード | 誤検知リスク | コメントノードとして分離済み |
| 文字列リテラル内の記号 | 誤検知リスク | 文字列ノードとして分離済み |
| `_` 継続行をまたぐ式 | 脆弱 | 構造として把握 |

---

## 依存ライブラリ

### Go
| ライブラリ | 用途 |
|------------|------|
| `github.com/antlr4-go/antlr/v4` | ANTLR4 Go ランタイム |
| `github.com/tliron/glsp` | LSP プロトコル実装フレームワーク |

### Python（TLB 抽出スクリプト）
| ライブラリ | 用途 |
|------------|------|
| `comtypes` または `pywin32` | Windows COM / TLB 読み込み |

---

## 流用資産

| 資産 | ライセンス | 用途 |
|------|-----------|------|
| SSlinky/VBA-LanguageServer の `vba.g4` | MIT | MS-VBAL 仕様準拠の VBA 文法定義（55KB） |
| `tools/vbafmt/keywords.go` | 本プロジェクト | キーワードリストの再利用 |
| `tools/vbafmt/segments.go` | 本プロジェクト | コメント・文字列の分離ロジック参考 |

---

## ディレクトリ構成（最終形）

```
tools/
  vbafmt/          # 既存フォーマッタ・Lint（Go）
  vba-ls/          # 新規 LSP サーバー（Go）
    antlr/         # vba.g4 + 生成済みパーサーコード
    server.go
    parser.go
    symbols.go
    completion.go
    diagnostics.go
  tlb-extractor/   # TLB → JSON 変換スクリプト（Python）
    extract.py
    catia_types.json  # 生成物

src/
  extension.ts     # 既存（LSP クライアント起動処理を追加）
  lspClient.ts     # 新規（LSP クライアント設定）
  formatter.ts     # 既存
  linter.ts        # 既存

bin/
  vbafmt.exe       # 既存
  vba-ls.exe       # 新規
```

---

## 検証方法

1. `tools/tlb-extractor/extract.py` を実行し、`catia_types.json` に `PartDocument` 等の型情報が含まれることを確認
2. `vba-ls.exe` を単体起動し、LSP initialize → `textDocument/completion` リクエストで補完候補が返ることを確認
3. VSCode で `.bas` ファイルを開き、以下を確認:
   - `Dim oDoc As PartDocument` 後に `oDoc.` で補完候補が表示される
   - `oDoc.InvalidProp` に診断エラーが表示される
   - `oDoc.Part` にホバーでシグネチャが表示される
