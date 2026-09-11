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

	// Max body -> 1MB
	r.Body = http.MaxBytesReader(w, r.Body, 1048576)

	var payload model.ChatRequest
	err := json.NewDecoder(r.Body).Decode(&payload)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when reading request body", slog.Any("err", err))
		if errors.Is(err, io.EOF) {
			http.Error(w, "request body cannot be empty", http.StatusBadRequest)
			return
		}
		http.Error(w, "malformed JSON payload: "+err.Error(), http.StatusBadRequest)
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
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	req, err := http.NewRequest("POST", DEEPSEEK_BASE_URL+"/chat/completions", bytes.NewBuffer(reqBodyJSON))
	if err != nil {
		h.logger.ErrorContext(ctx, "error when creating new http request", slog.Any("err", err))
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	req.Header.Set("Authorization", "Bearer "+h.APIKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.apiClient.Do(req)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when request chat completions to deepseek", slog.Any("err", err))
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		h.logger.ErrorContext(ctx, "error when read response chat completions from deepseek", slog.Any("err", err))
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	h.logger.InfoContext(ctx, "response chat completions from deepseek",
		slog.Int("status_code", resp.StatusCode),
		slog.String("status", resp.Status),
		slog.String("body", string(respBody)),
	)

	w.WriteHeader(resp.StatusCode)
	w.Write(respBody)
}
