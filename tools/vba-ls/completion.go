package main

import (
	"regexp"
	"strings"
)

// dotPattern はカーソル前の "識別子." パターンを検出する。
var dotPattern = regexp.MustCompile(`(\w+)\.$`)

// BuildCompletions はカーソル前テキストを解析して補完候補を返す。
func BuildCompletions(linePrefix string, table *SymbolTable, db *TLBDatabase, scope string) []CompletionItem {
	m := dotPattern.FindStringSubmatch(linePrefix)
	if m == nil {
		return nil
	}
	varName := m[1]

	sym := table.FindByName(varName, scope)
	if sym == nil || sym.TypeName == "" {
		return nil
	}

	members := db.Members(sym.TypeName)
	items := make([]CompletionItem, 0, len(members))
	for _, mem := range members {
		sig := mem.Signature()
		kind := CompletionItemKindProperty
		if mem.Kind == MemberKindMethod {
			kind = CompletionItemKindMethod
		}
		items = append(items, CompletionItem{
			Label:  mem.Name,
			Kind:   kind,
			Detail: sig,
		})
	}
	return items
}

// ResolveHoverMarkdown はホバー対象のシンボル情報を Markdown 文字列で返す。
func ResolveHoverMarkdown(wordAtCursor, prevWord string, table *SymbolTable, db *TLBDatabase, scope string) string {
	if prevWord == "" {
		sym := table.FindByName(wordAtCursor, scope)
		if sym == nil {
			return ""
		}
		return "**" + sym.Name + "** As " + sym.TypeName
	}

	sym := table.FindByName(prevWord, scope)
	if sym == nil || sym.TypeName == "" {
		return ""
	}
	members := db.Members(sym.TypeName)
	lword := strings.ToLower(wordAtCursor)
	for _, m := range members {
		if strings.ToLower(m.Name) == lword {
			return "```vba\n" + m.Signature() + "\n```"
		}
	}
	return ""
}
