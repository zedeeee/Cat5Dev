package main

import (
	"encoding/json"
	"os"
	"strings"
)

// TLBParam はメソッドのパラメータ情報。
type TLBParam struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// TLBProperty はクラスのプロパティ情報。
type TLBProperty struct {
	Name       string `json:"name"`
	ReturnType string `json:"returnType"`
}

// TLBMethod はクラスのメソッド情報。
type TLBMethod struct {
	Name       string     `json:"name"`
	ReturnType string     `json:"returnType"`
	Params     []TLBParam `json:"params"`
}

// TLBType はクラス・インターフェース1型の情報。
type TLBType struct {
	Kind       string        `json:"kind"`
	Properties []TLBProperty `json:"properties"`
	Methods    []TLBMethod   `json:"methods"`
}

// TLBDatabase は catia_types.json 全体を保持するデータベース。
// キー: 型名（大文字小文字は正規化して保持）
type TLBDatabase struct {
	types map[string]TLBType // 小文字キーで格納
	raw   map[string]string  // 小文字キー → 元の型名
}

// LoadTLBDatabase は JSON ファイルを読み込んでデータベースを返す。
func LoadTLBDatabase(path string) (*TLBDatabase, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var raw map[string]TLBType
	if err := json.NewDecoder(f).Decode(&raw); err != nil {
		return nil, err
	}

	db := &TLBDatabase{
		types: make(map[string]TLBType, len(raw)),
		raw:   make(map[string]string, len(raw)),
	}
	for k, v := range raw {
		lower := strings.ToLower(k)
		db.types[lower] = v
		db.raw[lower] = k
	}
	return db, nil
}

// LookupType は型名から TLBType を返す（大文字小文字を無視）。
func (db *TLBDatabase) LookupType(typeName string) (TLBType, bool) {
	t, ok := db.types[strings.ToLower(typeName)]
	return t, ok
}

// Members は型名に対するプロパティ＋メソッドの統合メンバー一覧を返す。
func (db *TLBDatabase) Members(typeName string) []MemberInfo {
	t, ok := db.LookupType(typeName)
	if !ok {
		return nil
	}
	var members []MemberInfo
	for _, p := range t.Properties {
		members = append(members, MemberInfo{
			Name:       p.Name,
			ReturnType: p.ReturnType,
			Kind:       MemberKindProperty,
		})
	}
	for _, m := range t.Methods {
		members = append(members, MemberInfo{
			Name:       m.Name,
			ReturnType: m.ReturnType,
			Kind:       MemberKindMethod,
			Params:     m.Params,
		})
	}
	return members
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
