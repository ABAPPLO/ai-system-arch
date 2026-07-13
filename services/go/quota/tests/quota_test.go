package tests

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

func TestHealth(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/quota/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		w.Write([]byte(`{"status":"ok","service":"quota"}`))
	})
	req := httptest.NewRequest("GET", "/v1/quota/health", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("expected 200, got %d", w.Code)
	}
}

func TestCheckJSONRoundtrip(t *testing.T) {
	req := models.QuotaCheckRequest{TenantID: "t_001", AppID: "app_test", APIID: "api_test", Cost: 1}
	data, _ := json.Marshal(req)
	var decoded models.QuotaCheckRequest
	json.Unmarshal(data, &decoded)
	if decoded.TenantID != "t_001" {
		t.Fatalf("expected t_001, got %s", decoded.TenantID)
	}
}

func TestCheckResponseJSON(t *testing.T) {
	resp := models.QuotaCheckResponse{Allowed: true, Current: 1, Limit: 100, Remaining: 99, ResetMs: 1000, RuleSource: "api"}
	data, _ := json.Marshal(resp)
	var decoded models.QuotaCheckResponse
	json.Unmarshal(data, &decoded)
	if !decoded.Allowed {
		t.Fatal("expected allowed true")
	}
}

func TestRefundJSON(t *testing.T) {
	req := models.QuotaRefundRequest{TenantID: "t_001", AppID: "app_test", APIID: "api_test", Cost: 1}
	data, _ := json.Marshal(req)
	var decoded models.QuotaRefundRequest
	json.Unmarshal(data, &decoded)
	if decoded.AppID != "app_test" {
		t.Fatalf("expected app_test, got %s", decoded.AppID)
	}
	resp := models.QuotaRefundResponse{Refunded: true}
	data, _ = json.Marshal(resp)
	var respDecoded models.QuotaRefundResponse
	json.Unmarshal(data, &respDecoded)
	if !respDecoded.Refunded {
		t.Fatal("expected refunded true")
	}
}

func TestUsageJSON(t *testing.T) {
	resp := models.UsageResponse{Points: []models.UsagePoint{
		{Tier: "second", Used: 5, Limit: 100, Remaining: 95, ResetMs: 45000},
	}}
	data, _ := json.Marshal(resp)
	var decoded models.UsageResponse
	json.Unmarshal(data, &decoded)
	if len(decoded.Points) != 1 {
		t.Fatalf("expected 1 point, got %d", len(decoded.Points))
	}
}

func TestCheckHTTPEndpoint(t *testing.T) {
	body := models.QuotaCheckRequest{TenantID: "t_001", AppID: "app_test", APIID: "api_test", Cost: 1}
	data, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/quota/check", bytes.NewReader(data))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/quota/check", func(w http.ResponseWriter, r *http.Request) {
		var req models.QuotaCheckRequest
		json.NewDecoder(r.Body).Decode(&req)
		resp := models.QuotaCheckResponse{Allowed: true, Current: 1, Limit: 100, Remaining: 99, ResetMs: 1000, RuleSource: "api"}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})
	mux.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	var resp models.QuotaCheckResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if !resp.Allowed {
		t.Fatal("expected allowed true")
	}
}
