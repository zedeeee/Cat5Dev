package main

import (
	"database/sql"
	"fmt"
	"strings"

	_ "modernc.org/sqlite"
)

// TLBParam はメソッドのパラメータ情報。
type TLBParam struct {
	Name string
	Type string
}

// MemberKind はメンバーの種類。
type MemberKind int

const (
	MemberKindProperty MemberKind = iota
	MemberKindMethod
)

// MemberInfo は補完候補1件の情報。
type MemberInfo struct {
	Name       string
	ReturnType string
	Kind       MemberKind
	Params     []TLBParam
}

// Signature はホバー表示用のシグネチャ文字列を生成する。
func (m MemberInfo) Signature() string {
	if m.Kind == MemberKindProperty {
		return m.Name + " As " + m.ReturnType
	}
	params := make([]string, len(m.Params))
	for i, p := range m.Params {
		params[i] = p.Name + " As " + p.Type
	}
	ret := ""
	if m.ReturnType != "" && m.ReturnType != "void" {
		ret = " As " + m.ReturnType
	}
	return m.Name + "(" + strings.Join(params, ", ") + ")" + ret
}

// TLBDatabase は catia_api.db へのアクセスを提供する。
type TLBDatabase struct {
	db *sql.DB
}

// LoadTLBDatabase は SQLite ファイルを開いてデータベースを返す。
func LoadTLBDatabase(path string) (*TLBDatabase, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("SQLite open: %w", err)
	}
	// 読み取り専用で使うため接続数は1で十分
	db.SetMaxOpenConns(1)
	return &TLBDatabase{db: db}, nil
}

// Close はデータベース接続を閉じる。
func (t *TLBDatabase) Close() {
	if t.db != nil {
		_ = t.db.Close()
	}
}

// Members は型名（継承チェーン含む）のメンバー一覧を返す。
// Cat5Dev2 と同じ再帰 CTE を使用。
func (t *TLBDatabase) Members(typeName string) []MemberInfo {
	const q = `
WITH RECURSIVE parents(id, level) AS (
    SELECT id, 0 FROM interfaces WHERE name = ? COLLATE NOCASE
    UNION ALL
    SELECT i.id, p.level + 1
    FROM interfaces i
    JOIN parents p ON i.id = (
        SELECT parent_id FROM interfaces WHERE id = p.id
    )
    WHERE p.level < 20
)
SELECT m.name, m.return_type, 0 AS is_prop, '' AS params_placeholder
FROM methods m
WHERE m.interface_id IN (SELECT id FROM parents)
UNION ALL
SELECT p.name, p.type, 1, ''
FROM properties p
WHERE p.interface_id IN (SELECT id FROM parents)
ORDER BY 1;`

	rows, err := t.db.Query(q, typeName)
	if err != nil {
		return nil
	}
	defer rows.Close()

	var items []MemberInfo
	for rows.Next() {
		var name, retType, placeholder string
		var isProp int
		if err := rows.Scan(&name, &retType, &isProp, &placeholder); err != nil {
			continue
		}
		kind := MemberKindMethod
		if isProp == 1 {
			kind = MemberKindProperty
		}
		items = append(items, MemberInfo{
			Name:       name,
			ReturnType: retType,
			Kind:       kind,
		})
	}

	// メソッドのパラメータを別クエリで取得（まとめて取得してマージ）
	t.fillParams(items, typeName)
	return items
}

// fillParams は Members で取得したメソッドのパラメータを補完する。
func (t *TLBDatabase) fillParams(items []MemberInfo, typeName string) {
	const q = `
WITH RECURSIVE parents(id, level) AS (
    SELECT id, 0 FROM interfaces WHERE name = ? COLLATE NOCASE
    UNION ALL
    SELECT i.id, p.level + 1
    FROM interfaces i
    JOIN parents p ON i.id = (SELECT parent_id FROM interfaces WHERE id = p.id)
    WHERE p.level < 20
)
SELECT m.name, pa.name, pa.type
FROM methods m
JOIN parameters pa ON pa.method_id = m.id
WHERE m.interface_id IN (SELECT id FROM parents)
ORDER BY m.name, pa.position;`

	rows, err := t.db.Query(q, typeName)
	if err != nil {
		return
	}
	defer rows.Close()

	// メソッド名 → インデックスのマップ
	idx := make(map[string]int)
	for i, item := range items {
		if item.Kind == MemberKindMethod {
			idx[strings.ToLower(item.Name)] = i
		}
	}

	for rows.Next() {
		var methodName, paramName, paramType string
		if err := rows.Scan(&methodName, &paramName, &paramType); err != nil {
			continue
		}
		if i, ok := idx[strings.ToLower(methodName)]; ok {
			items[i].Params = append(items[i].Params, TLBParam{
				Name: paramName,
				Type: paramType,
			})
		}
	}
}

// GetReturnType はオブジェクトのメンバーアクセス時の戻り値型を返す。
// 補完チェーン解決（oDoc.Part.Bodies 等）に使用。
func (t *TLBDatabase) GetReturnType(typeName, memberName string) string {
	const q = `
WITH RECURSIVE parents(id, level) AS (
    SELECT id, 0 FROM interfaces WHERE name = ? COLLATE NOCASE
    UNION ALL
    SELECT i.id, p.level + 1
    FROM interfaces i
    JOIN parents p ON i.id = (SELECT parent_id FROM interfaces WHERE id = p.id)
    WHERE p.level < 20
)
SELECT return_type FROM methods
WHERE interface_id IN (SELECT id FROM parents) AND name = ? COLLATE NOCASE
UNION ALL
SELECT type FROM properties
WHERE interface_id IN (SELECT id FROM parents) AND name = ? COLLATE NOCASE
LIMIT 1;`

	var ret string
	_ = t.db.QueryRow(q, typeName, memberName, memberName).Scan(&ret)
	return ret
}

// HasMember は型名が指定メンバーを持つか（継承含む）確認する。
func (t *TLBDatabase) HasMember(typeName, memberName string) bool {
	const q = `
WITH RECURSIVE parents(id, level) AS (
    SELECT id, 0 FROM interfaces WHERE name = ? COLLATE NOCASE
    UNION ALL
    SELECT i.id, p.level + 1
    FROM interfaces i
    JOIN parents p ON i.id = (SELECT parent_id FROM interfaces WHERE id = p.id)
    WHERE p.level < 20
)
SELECT COUNT(*) FROM (
    SELECT 1 FROM methods   WHERE interface_id IN (SELECT id FROM parents) AND name = ? COLLATE NOCASE
    UNION ALL
    SELECT 1 FROM properties WHERE interface_id IN (SELECT id FROM parents) AND name = ? COLLATE NOCASE
) LIMIT 1;`

	var count int
	_ = t.db.QueryRow(q, typeName, memberName, memberName).Scan(&count)
	return count > 0
}

// TypeExists は型名が DB に存在するか確認する。
func (t *TLBDatabase) TypeExists(typeName string) bool {
	var count int
	_ = t.db.QueryRow(
		"SELECT COUNT(*) FROM interfaces WHERE name = ? COLLATE NOCASE", typeName,
	).Scan(&count)
	return count > 0
}
