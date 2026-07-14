package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"traffic-stats/backend/internal/dashboard"
	"traffic-stats/backend/internal/testfixture"
)

// TestFrontendServesAssetsAndSpaFallback 验证静态资源优先于客户端路由回退。
func TestFrontendServesAssetsAndSpaFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	directory := t.TempDir()
	if err := os.Mkdir(filepath.Join(directory, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "index.html"), []byte("dashboard"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "assets", "app.js"), []byte("asset"), 0o644); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	router.NoRoute(frontend(directory))

	assetRequest := httptest.NewRequest(http.MethodGet, "/assets/app.js", nil)
	assetResponse := httptest.NewRecorder()
	router.ServeHTTP(assetResponse, assetRequest)
	if assetResponse.Code != http.StatusOK || assetResponse.Body.String() != "asset" {
		t.Fatalf("asset response = %d %q", assetResponse.Code, assetResponse.Body.String())
	}

	pageRequest := httptest.NewRequest(http.MethodGet, "/devices/1:1", nil)
	pageResponse := httptest.NewRecorder()
	router.ServeHTTP(pageResponse, pageRequest)
	if pageResponse.Code != http.StatusOK || pageResponse.Body.String() != "dashboard" {
		t.Fatalf("fallback response = %d %q", pageResponse.Code, pageResponse.Body.String())
	}
}

// TestStatusIncludesDatabaseMetadata 验证公开状态响应为仪表盘提供不落盘的数据库字段。
func TestStatusIncludesDatabaseMetadata(t *testing.T) {
	gin.SetMode(gin.TestMode)
	databasePath := testfixture.CreateTrafficDatabase(t)
	store, err := dashboard.Open(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	info, err := os.Stat(databasePath)
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodGet, "/api/v1/status", nil)
	response := httptest.NewRecorder()
	New(store, "").ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status response = %d %q", response.Code, response.Body.String())
	}
	var status dashboard.ServiceStatus
	if err := json.Unmarshal(response.Body.Bytes(), &status); err != nil {
		t.Fatal(err)
	}
	if status.DatabaseBytes != info.Size() || !status.DatabaseAvailable {
		t.Fatalf("unexpected status payload: %#v", status)
	}
}

// TestDashboardRejectsInvalidRange 验证不支持的时间范围返回客户端错误而非服务不可用。
func TestDashboardRejectsInvalidRange(t *testing.T) {
	gin.SetMode(gin.TestMode)
	databasePath := testfixture.CreateTrafficDatabase(t)
	store, err := dashboard.Open(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })

	request := httptest.NewRequest(http.MethodGet, "/api/v1/dashboard?range=unsupported", nil)
	response := httptest.NewRecorder()
	New(store, "").ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || response.Body.String() != "{\"error\":\"invalid_range\"}" {
		t.Fatalf("dashboard response = %d %q", response.Code, response.Body.String())
	}
}
