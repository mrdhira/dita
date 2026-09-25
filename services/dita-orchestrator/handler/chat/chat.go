package chat

import (
	"bytes"
	"dita-orchestrator/model"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
)

func (h *ChatHandler) Chat(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	if !configured(h.APIKey) {
		refuse(w, http.StatusServiceUnavailable, "Unhealthy", "chat is not configured: DEEPSEEK_API_KEY is missing or a placeholder")
		return
	}

	// Max body -> 1MB
	r.Body = http.MaxBytesReader(w, r.Body, 1048576)

	var payload model.ChatRequest
	err := json.NewDecoder(r.Body).Decode(&payload)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when reading request body", slog.Any("err", err))
		if errors.Is(err, io.EOF) {
			refuse(w, http.StatusBadRequest, "Validation", "request body cannot be empty")
			return
		}
		refuse(w, http.StatusBadRequest, "Validation", "malformed JSON payload: "+err.Error())
		return
	}

	reqBody := model.ChatCompletionsRequest{
		Model: DEEPSEEK_MODEL_FLASH,
		Messages: []model.Message{
			{
				Role:    "system",
				Content: "You are an JARVIS like AI called Dita.",
			},
			{
				Role:    "user",
				Content: payload.Message,
			},
		},
		Thinking: model.Thinking{
			Type: "disabled",
		},
		ReasoningEffort: "low",
		Stream:          false,
	}

	reqBodyJSON, err := json.Marshal(reqBody)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when marshal request json", slog.Any("err", err))
		refuse(w, http.StatusInternalServerError, "Backend", "internal server error")
		return
	}

	req, err := http.NewRequest("POST", h.baseURL+"/chat/completions", bytes.NewBuffer(reqBodyJSON))
	if err != nil {
		h.logger.ErrorContext(ctx, "error when creating new http request", slog.Any("err", err))
		refuse(w, http.StatusInternalServerError, "Backend", "internal server error")
		return
	}

	req.Header.Set("Authorization", "Bearer "+h.APIKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.apiClient.Do(req)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when request chat completions to deepseek", slog.Any("err", err))
		refuse(w, http.StatusInternalServerError, "Backend", "internal server error")
		return
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when read response chat completions from deepseek", slog.Any("err", err))
		refuse(w, http.StatusInternalServerError, "Backend", "internal server error")
		return
	}

	// Status and size only: a reply can quote what was asked, and container logs are readable on the LAN.
	h.logger.InfoContext(ctx, "response chat completions from deepseek",
		slog.Int("status_code", resp.StatusCode),
		slog.Int("bytes", len(respBody)),
	)

	w.WriteHeader(resp.StatusCode)
	w.Write(respBody)
}

// refuse answers in the error shape every other route uses: {error, error_type}.
func refuse(w http.ResponseWriter, status int, errorType, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message, "error_type": errorType})
}
