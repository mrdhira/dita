package model

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type Thinking struct {
	Type string `json:"type"`
}

type Choice struct {
	Index        int8    `json:"index"`
	Message      Message `json:"message"`
	Logprobs     any     `json:"logprobs"`
	FinishReason string  `json:"finish_reason"`
}

type Usage struct {
	PromptTokens        int32 `json:"prompt_tokens"`
	CompletionTokens    int32 `json:"completion_tokens"`
	TotalToken          int32 `json:"total_tokens"`
	PromptTokensDetails struct {
		CachedTokens int32 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	PromptCacheHitTokens  int32 `json:"prompt_cache_hit_tokens"`
	PromptCacheMissTokens int32 `json:"prompt_cache_miss_tokens"`
}

type ChatCompletionsRequest struct {
	Model           string    `json:"model"`
	Messages        []Message `json:"messages"`
	Thinking        Thinking  `json:"thinking"`
	ReasoningEffort string    `json:"reasoning_effort"`
	Stream          bool      `json:"stream"`
}

type ChatCompletionsResponse struct {
	ID      string   `json:"id"`
	Object  string   `json:"object"`
	Created int32    `json:"created"`
	Model   string   `json:"model"`
	Choices []Choice `json:"choices"`
}
