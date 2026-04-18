package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/sourcegraph/jsonrpc2"
)

func main() {
	db := loadDB()

	handler := newLSPHandler(db)
	codec := jsonrpc2.VSCodeObjectCodec{}
	stream := jsonrpc2.NewBufferedStream(stdinStdout{}, codec)
	conn := jsonrpc2.NewConn(context.Background(), stream, handler)
	<-conn.DisconnectNotify()
}

// stdinStdout は os.Stdin / os.Stdout を ReadWriteCloser として提供する。
type stdinStdout struct{}

func (stdinStdout) Read(p []byte) (int, error)  { return os.Stdin.Read(p) }
func (stdinStdout) Write(p []byte) (int, error) { return os.Stdout.Write(p) }
func (stdinStdout) Close() error                { return nil }

func loadDB() *TLBDatabase {
	path := findTLBDatabase()
	if path != "" {
		db, err := LoadTLBDatabase(path)
		if err == nil {
			db.EnsureScriptingTypes()
			return db
		}
		fmt.Fprintf(os.Stderr, "vba-ls: TLBデータベース読み込みエラー: %v\n", err)
	}
	fmt.Fprintf(os.Stderr, "vba-ls: catia_api.db が見つかりません。補完は無効です。\n")
	fmt.Fprintf(os.Stderr, "  tools/tlb-extractor/ で uv run python extract.py を実行してください。\n")
	return nil
}

func findTLBDatabase() string {
	if p := os.Getenv("CAT5DEV_TLB_DB"); p != "" {
		return p
	}
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	// bin/ と同じディレクトリ
	for _, name := range []string{"catia_api.db", "catia_types.db"} {
		candidate := filepath.Join(filepath.Dir(exe), name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	// 開発時: tools/tlb-extractor/catia_api.db
	dev := filepath.Join(filepath.Dir(exe), "..", "..", "tools", "tlb-extractor", "catia_api.db")
	if _, err := os.Stat(dev); err == nil {
		return dev
	}
	return ""
}
