package chat

import (
	"log/slog"
	"strings"

	"github.com/Wigata-Intech/w-tools/httpx/client"
)

const (
	DEEPSEEK_BASE_URL    = "https://api.deepseek.com"
	DEEPSEEK_MODEL_FLASH = "deepseek-flash"
	DEEPSEEK_MODEL_PRO   = "deepseek-v4-pro"
)

type ChatHandler struct {
	APIKey    string
	logger    *slog.Logger
	apiClient *client.Client
	// baseURL is DEEPSEEK_BASE_URL; a test points it at a local server.
	baseURL string
}

func New(APIKey string, logger *slog.Logger) *ChatHandler {
	apiClient := client.New(client.Config{Log: logger})

	return &ChatHandler{APIKey, logger, apiClient, DEEPSEEK_BASE_URL}
}

// placeholders are values a key is set to when nobody meant it to spend money.
var placeholders = []string{"unused", "changeme", "change-me", "placeholder", "dummy", "example", "xxx", "your", "todo"}

// configured reports whether key could be a real DeepSeek key: not blank, not padded, and not
// one of the stand-ins a compose file or a test would use.
func configured(key string) bool {
	if strings.TrimSpace(key) == "" || strings.TrimSpace(key) != key {
		return false
	}
	lower := strings.ToLower(key)
	for _, p := range placeholders {
		if strings.Contains(lower, p) {
			return false
		}
	}
	return true
}
