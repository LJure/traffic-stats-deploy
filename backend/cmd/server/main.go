package main

import (
	"log"
	"os"

	"github.com/gin-gonic/gin"
	"traffic-stats/backend/internal/dashboard"
	"traffic-stats/backend/internal/httpapi"
)

// main 读取部署配置，打开只读统计存储并启动 HTTP API。
func main() {
	// 默认仅监听回环地址，由本机反向代理或 Cloudflare Tunnel 对外发布。
	databasePath := getenv("TRAFFIC_STATS_DB", "/var/lib/traffic-stats/traffic.sqlite3")
	listenAddress := getenv("TRAFFIC_STATS_LISTEN", "127.0.0.1:8788")
	staticDirectory := os.Getenv("TRAFFIC_STATS_STATIC_DIR")

	store, err := dashboard.Open(databasePath)
	if err != nil {
		log.Fatalf("open traffic statistics database: %v", err)
	}
	defer store.Close()

	gin.SetMode(gin.ReleaseMode)
	router := httpapi.New(store, staticDirectory)
	// 服务不信任转发请求头，避免未配置代理白名单时伪造客户端来源地址。
	if err := router.SetTrustedProxies(nil); err != nil {
		log.Fatalf("configure trusted proxies: %v", err)
	}
	log.Printf("traffic-stats API listening on %s", listenAddress)
	if err := router.Run(listenAddress); err != nil {
		log.Fatalf("serve traffic statistics API: %v", err)
	}
}

// getenv 优先返回已设置的环境变量，否则保留安全的部署默认值。
func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
