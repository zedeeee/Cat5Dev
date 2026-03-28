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
	conn := jsonrpc2.NewConn(context.Background(), stream, jsonrpc2.HandlerWithError(
		func(ctx context.Context, conn *jsonrpc2.Conn, req *jsonrpc2.Request) (interface{}, error) {
			handler.Handle(ctx, conn, req)
			return nil, nil
		},
	))
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
			return db
		}
		fmt.Fprintf(os.Stderr, "vba-ls: TLBデータベース読み込みエラー: %v\n", err)
	}
	fmt.Fprintf(os.Stderr, "vba-ls: catia_types.json が見つかりません。補完は無効です。\n")
	return &TLBDatabase{
		types: make(map[string]TLBType),
		raw:   make(map[string]string),
	}
}

func findTLBDatabase() string {
	if p := os.Getenv("CAT5DEV_TLB_JSON"); p != "" {
		return p
	}
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	candidate := filepath.Join(filepath.Dir(exe), "catia_types.json")
	if _, err := os.Stat(candidate); err == nil {
		return candidate
	}
	dev := filepath.Join(filepath.Dir(exe), "..", "..", "tools", "tlb-extractor", "catia_types.json")
	if _, err := os.Stat(dev); err == nil {
		return dev
	}
	return ""
}
