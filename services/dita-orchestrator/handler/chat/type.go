package chat

import (
	"log/slog"

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
}

func New(APIKey string, logger *slog.Logger) *ChatHandler {
	apiClient := client.New(client.Config{Log: logger})

	return &ChatHandler{APIKey, logger, apiClient}
}
