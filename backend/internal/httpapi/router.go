package httpapi

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"traffic-stats/backend/internal/dashboard"
)

// New 创建带版本号的 JSON API，并可选地托管已编译的单页前端。
func New(store *dashboard.Store, staticDirectory string) *gin.Engine {
	router := gin.New()
	router.Use(gin.Logger(), gin.Recovery())
	// 状态接口不依赖前端快照，用于页面轮询和服务状态展示。
	router.GET("/api/v1/status", func(c *gin.Context) {
		status, err := store.Status(c.Request.Context())
		if err != nil {
			fail(c, http.StatusServiceUnavailable, "collector_status_unavailable")
			return
		}
		c.JSON(http.StatusOK, status)
	})
	// 设备列表和仪表盘共用 snapshot，保证一次前端刷新中的数据视图一致。
	router.GET("/api/v1/devices", func(c *gin.Context) {
		snapshot, ok := snapshot(c)
		if !ok {
			return
		}
		devices, err := store.Devices(c.Request.Context(), snapshot)
		if err != nil {
			fail(c, http.StatusServiceUnavailable, "devices_unavailable")
			return
		}
		c.JSON(http.StatusOK, gin.H{"devices": devices})
	})
	// 自选时间范围的校验错误明确返回 400，其余存储错误统一视为暂时不可用。
	router.GET("/api/v1/dashboard", func(c *gin.Context) {
		snapshot, ok := snapshot(c)
		if !ok {
			return
		}
		result, err := store.Dashboard(c.Request.Context(), c.Query("range"), c.Query("device"), c.Query("start"), c.Query("end"), snapshot)
		if err != nil {
			if errors.Is(err, dashboard.ErrInvalidRange) {
				fail(c, http.StatusBadRequest, "invalid_range")
				return
			}
			fail(c, http.StatusServiceUnavailable, "dashboard_unavailable")
			return
		}
		c.JSON(http.StatusOK, result)
	})
	if staticDirectory != "" {
		// 生产环境由同一进程托管静态资源和 API，减少额外的反向代理配置。
		router.NoRoute(frontend(staticDirectory))
	} else {
		router.NoRoute(func(c *gin.Context) { fail(c, http.StatusNotFound, "not_found") })
	}
	return router
}

// frontend 优先返回已编译资源，并为前端路由回退到 SPA 入口文件。
func frontend(staticDirectory string) gin.HandlerFunc {
	return func(c *gin.Context) {
		requested := strings.TrimPrefix(filepath.ToSlash(filepath.Clean(c.Request.URL.Path)), "/")
		if requested != "" && requested != "." {
			candidate := filepath.Join(staticDirectory, filepath.FromSlash(requested))
			relative, err := filepath.Rel(staticDirectory, candidate)
			if err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
				// 相对路径必须仍位于静态目录内，避免通过 URL 读取目录外文件。
				if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
					c.File(candidate)
					return
				}
			}
		}
		// 未命中静态文件时交给 Vue 路由处理。
		c.File(filepath.Join(staticDirectory, "index.html"))
	}
}

// snapshot 解析可选的不可变数据截止时间，并拒绝格式错误的客户端输入。
func snapshot(c *gin.Context) (int64, bool) {
	raw := c.Query("snapshot")
	if raw == "" {
		return 0, true
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value < 0 {
		fail(c, http.StatusBadRequest, "invalid_snapshot")
		return 0, false
	}
	return value, true
}

// fail 使用统一且精简的 JSON 错误结构结束请求。
func fail(c *gin.Context, status int, code string) {
	c.AbortWithStatusJSON(status, gin.H{"error": code})
}
