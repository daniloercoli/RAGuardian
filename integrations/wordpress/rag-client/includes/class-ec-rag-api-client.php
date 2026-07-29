<?php
/**
 * EC_Rag_Api_Client
 *
 * HTTP client for RAGuardian API calls.
 * Handles GET, POST, multipart POST, DELETE with exponential backoff retry.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Api_Client {

    // Maximum retry attempts (excluding the initial request).
    const MAX_RETRIES = 3;

    /** Initial backoff delay in seconds. */
    const INITIAL_DELAY = 1;

    /** Settings-page catalog lookups must never inherit the normal request timeout. */
    const KNOWLEDGE_BASE_CATALOG_TIMEOUT = 3;

    /** Cache a successful authorized catalog for five minutes. */
    const KNOWLEDGE_BASE_CATALOG_TTL = 300;

    /** Briefly cache failures so an unavailable service does not stall every page load. */
    const KNOWLEDGE_BASE_CATALOG_ERROR_TTL = 30;

    /** The options array (injected via dependency). */
    public $options = [];

    /** The options callback. */
    public $get_options = null;

    /** Unique request ID for tracing across RAGuardian + WordPress. */
    public $request_id = '';

    /**
     * Constructor.
     *
     * @param callable $get_options Callback that returns the plugin options array.
     */
    public function __construct(callable $get_options) {
        $this->get_options = $get_options;
        $this->request_id  = wp_generate_uuid4();
        $this->options     = $get_options();
    }

    /**
     * Ensure base_url and api_key are configured.
     *
     * @return WP_Error|true WP_Error if not configured, true otherwise.
     */
    public function ensure_configured() {
        $this->options = ($this->get_options)();

        if (empty($this->options['base_url']) || empty($this->options['api_key'])) {
            return new WP_Error('ec_rag_not_configured', __('RAG client is not configured', 'ec-rag'));
        }

        return true;
    }

    /**
     * GET request with retry logic.
     *
     * @param string $path The API path (e.g. /api/v1/health).
     * @param bool   $binary Whether to return raw binary body on success.
     * @return array|WP_Error|null
     */
    public function get(string $path, bool $binary = false) {
        $result = $this->ensure_configured();
        if (is_wp_error($result)) {
            return $result;
        }

        if (strpos($path, '/api/v1/health') === 0) {
            $path = $this->append_knowledge_base_query($path);
        }
        $url = $this->options['base_url'] . $path;
        $args = [
            'timeout' => absint($this->options['request_timeout'] ?? 45),
            'blocking' => true,
            'headers' => [
                'X-API-Key'    => $this->options['api_key'],
                'X-Request-ID' => $this->request_id,
            ],
        ];

        $response = $this->request_with_retry('GET', $url, $args, $binary);

        $this->options = ($this->get_options)();
        return $response;
    }

    /**
     * Fetch the API-key-authorized knowledge-base catalog for the settings UI.
     *
     * Unlike normal API traffic, this lookup is short, single-attempt, and
     * cached by endpoint + API key. A failed lookup is also cached briefly so
     * repeatedly rendering wp-admin cannot multiply a network outage into a
     * long delay.
     *
     * @return array|WP_Error
     */
    public function get_knowledge_base_catalog() {
        $result = $this->ensure_configured();
        if (is_wp_error($result)) {
            return $result;
        }

        $cache_key = $this->knowledge_base_catalog_cache_key();
        $cached = get_transient($cache_key);
        if (is_array($cached) && array_key_exists('ok', $cached)) {
            if ($cached['ok'] && is_array($cached['payload'] ?? null)) {
                return $cached['payload'];
            }

            return new WP_Error(
                'ec_rag_catalog_unavailable',
                sanitize_text_field(
                    $cached['message'] ?? __('Knowledge-base catalog is unavailable', 'ec-rag')
                )
            );
        }

        $url = $this->options['base_url'] . '/api/v1/knowledge-bases';
        $args = [
            'timeout' => self::KNOWLEDGE_BASE_CATALOG_TIMEOUT,
            'blocking' => true,
            'headers' => [
                'X-API-Key'    => $this->options['api_key'],
                'X-Request-ID' => $this->request_id,
            ],
        ];
        $response = $this->do_request('GET', $url, $args);
        $decoded = self::decode_response($response);
        $valid_catalog = (
            is_array($decoded)
            && array_key_exists('knowledge_bases', $decoded)
            && is_array($decoded['knowledge_bases'])
        );

        if (is_wp_error($decoded) || !$valid_catalog) {
            $message = is_wp_error($decoded)
                ? $decoded->get_error_message()
                : __('Knowledge-base catalog returned an invalid response', 'ec-rag');
            set_transient(
                $cache_key,
                [
                    'ok'      => false,
                    'message' => sanitize_text_field($message),
                ],
                self::KNOWLEDGE_BASE_CATALOG_ERROR_TTL
            );

            return is_wp_error($decoded)
                ? $decoded
                : new WP_Error('ec_rag_invalid_catalog', $message);
        }

        set_transient(
            $cache_key,
            [
                'ok'      => true,
                'payload' => $decoded,
            ],
            self::KNOWLEDGE_BASE_CATALOG_TTL
        );

        return $decoded;
    }

    /**
     * JSON POST request with retry logic.
     *
     * @param string $path The API path.
     * @param array  $payload The JSON body payload.
     * @param bool   $binary Whether to return binary response (e.g. audio).
     * @return array|WP_Error
     */
    public function post(string $path, array $payload, bool $binary = false) {
        $result = $this->ensure_configured();
        if (is_wp_error($result)) {
            return $result;
        }

        if (
            strpos($path, '/api/v1/query') === 0
            && $this->knowledge_base_id() !== ''
        ) {
            $payload['knowledge_base_id'] = $this->knowledge_base_id();
        }
        $url = $this->options['base_url'] . $path;
        $args = [
            'timeout' => absint($this->options['request_timeout'] ?? 45),
            'blocking' => true,
            'headers' => [
                'Content-Type' => 'application/json',
                'X-API-Key'    => $this->options['api_key'],
                'X-Request-ID' => $this->request_id,
            ],
            'body'  => wp_json_encode($payload),
        ];

        $response = $this->request_with_retry('POST', $url, $args, $binary);

        $this->options = ($this->get_options)();
        return $response;
    }

    /**
     * Multipart form-data POST with retry logic.
     *
     * @param string $path The API path.
     * @param array  $fields Form fields.
     * @param array  $file File data: [filename, content_type, content].
     * @return array|WP_Error
     */
    public function post_multipart(string $path, array $fields, array $file) {
        $result = $this->ensure_configured();
        if (is_wp_error($result)) {
            return $result;
        }

        $boundary = 'ec-rag-' . wp_generate_uuid4();
        $body     = '';
        if (
            (
                strpos($path, '/api/v1/files') === 0
                || strpos($path, '/api/v1/audio') === 0
            )
            && $this->knowledge_base_id() !== ''
        ) {
            $fields['knowledge_base_id'] = $this->knowledge_base_id();
        }

        foreach ($fields as $name => $value) {
            $body .= '--' . $boundary . "\r\n";
            $body .= 'Content-Disposition: form-data; name="' . sanitize_key($name) . '"' . "\r\n\r\n";
            $body .= (string) $value . "\r\n";
        }

        $filename     = sanitize_file_name($file['filename'] ?? '');
        $content_type = $file['content_type'] ?? 'application/octet-stream';

        $body .= '--' . $boundary . "\r\n";
        $body .= 'Content-Disposition: form-data; name="file"; filename="' . $filename . '"' . "\r\n";
        $body .= 'Content-Type: ' . $content_type . "\r\n\r\n";
        $body .= $file['content'] . "\r\n";
        $body .= '--' . $boundary . "--\r\n";

        $url = $this->options['base_url'] . $path;
        $args = [
            'timeout' => absint($this->options['request_timeout'] ?? 45),
            'blocking' => true,
            'headers' => [
                'Content-Type' => 'multipart/form-data; boundary=' . $boundary,
                'X-API-Key'    => $this->options['api_key'],
                'X-Request-ID' => $this->request_id,
            ],
            'body' => $body,
        ];

        $response = $this->request_with_retry('POST', $url, $args);

        $this->options = ($this->get_options)();
        return $response;
    }

    /**
     * DELETE request with retry logic.
     *
     * @param string $path The API path.
     * @return array|WP_Error
     */
    public function delete(string $path) {
        $result = $this->ensure_configured();
        if (is_wp_error($result)) {
            return $result;
        }

        if (strpos($path, '/api/v1/files/') === 0) {
            $path = $this->append_knowledge_base_query($path);
        }
        $url = $this->options['base_url'] . $path;
        $args = [
            'method'  => 'DELETE',
            'timeout' => absint($this->options['request_timeout'] ?? 45),
            'blocking' => true,
            'headers' => [
                'X-API-Key'    => $this->options['api_key'],
                'X-Request-ID' => $this->request_id,
            ],
        ];

        $response = $this->request_with_retry('DELETE', $url, $args);

        $this->options = ($this->get_options)();
        return $response;
    }

    /**
     * Raw HTTP request with exponential backoff retry.
     *
     * Only retries on WP_Error (network failures) or 5xx responses.
     * Will not retry 4xx client errors.
     *
     * @param string $method HTTP verb.
     * @param string $url Full URL.
     * @param array  $args wp_remote_* args.
     * @param bool   $binary Decode as binary on success.
     * @return array|WP_Error
     */
    public function request_with_retry(string $method, string $url, array $args, bool $binary = false) {
        $delay = self::INITIAL_DELAY;
        $last_error = null;

        for ($attempt = 0; $attempt <= self::MAX_RETRIES; $attempt++) {
            $response = $this->do_request($method, $url, $args);

            if (!is_wp_error($response)) {
                $code = wp_remote_retrieve_response_code($response);

                // Retry only on 5xx (and transient 408, 429).
                if ($code >= 500 || $code === 408 || $code === 429) {
                    if ($attempt < self::MAX_RETRIES) {
                        $this->sleep_before_retry($delay, $attempt, $method, $url);
                        $delay *= 2;
                        continue;
                    }
                }

                return self::decode_response($response, $binary);
            }

            // WP_Error (network failure) - retry.
            $last_error = $response;
            if ($attempt < self::MAX_RETRIES) {
                $this->sleep_before_retry($delay, $attempt, $method, $url);
                $delay *= 2;
                continue;
            }

            EC_Rag_Logger::log(
                sprintf(
                    '%s %s failed after %d retries (%s)',
                    $method,
                    $url,
                    self::MAX_RETRIES,
                    $response->get_error_message()
                ),
                'api_request',
                2
            );

            return $response;
        }

        // Exceeded retries - log and fail.
        $error = is_wp_error($last_error)
            ? $last_error->get_error_message()
            : 'unknown HTTP error';

        EC_Rag_Logger::log(
            sprintf(
                '%s %s failed after %d retries (%s)',
                $method,
                $url,
                self::MAX_RETRIES,
                $error
            ),
            'api_request',
            2
        );

        return new WP_Error('ec_rag_request_failed', __('HTTP request failed after retries', 'ec-rag'));
    }

    /**
     * Return the server-side configured target. An empty value means default.
     *
     * @return string
     */
    protected function knowledge_base_id(): string {
        $value = sanitize_text_field($this->options['knowledge_base_id'] ?? '');

        return preg_match('/^kb_[0-9a-f]{32}$/', $value) ? $value : '';
    }

    /**
     * Append the configured target as a query parameter when non-default.
     *
     * @param string $path API path.
     * @return string
     */
    protected function append_knowledge_base_query(string $path): string {
        $knowledge_base_id = $this->knowledge_base_id();
        if ($knowledge_base_id === '') {
            return $path;
        }

        return add_query_arg('knowledge_base_id', $knowledge_base_id, $path);
    }

    /**
     * Return a credential-scoped transient key without exposing the API key.
     *
     * @return string
     */
    protected function knowledge_base_catalog_cache_key(): string {
        $identity = rtrim((string) $this->options['base_url'], '/')
            . "\0"
            . (string) $this->options['api_key'];

        return 'ec_rag_kb_catalog_' . hash('sha256', $identity);
    }

    /**
     * Sleep between retry attempts.
     *
     * @param int|float $delay Delay in seconds.
     * @param int       $attempt Retry attempt number.
     * @param string    $method HTTP method.
     * @param string    $url Full URL.
     * @return void
     */
    protected function sleep_before_retry($delay, int $attempt, string $method, string $url): void {
        $delay = apply_filters('ec_rag_api_retry_delay', $delay, $attempt, $method, $url);
        $delay = max(0, (float) $delay);

        if ($delay > 0) {
            usleep((int) ($delay * 1000000));
        }
    }

    /**
     * Execute a single HTTP request.
     *
     * @param string $method HTTP verb.
     * @param string $url Full URL.
     * @param array  $args Args for wp_remote*.
     * @return WP_HTTP_RequestsResponse|WP_Error
     */
    protected function do_request(string $method, string $url, array $args) {
        if ($method === 'GET') {
            return wp_remote_get($url, $args);
        }

        if ($method === 'POST') {
            return wp_remote_post($url, $args);
        }

        if ($method === 'DELETE') {
            return wp_remote_request($url, $args);
        }

        return new WP_Error('ec_rag_invalid_method', __('Invalid HTTP method', 'ec-rag'));
    }

    /**
     * Decode API response body.
     *
     * @param WP_HTTP_RequestsResponse $response The HTTP response.
     * @param bool                     $binary Binary mode (e.g. TTS audio).
     * @return array|WP_Error
     */
    public static function decode_response($response, bool $binary = false) {
        if (is_wp_error($response)) {
            return $response;
        }

        $code = wp_remote_retrieve_response_code($response);
        $body = wp_remote_retrieve_body($response);

        if ($code < 200 || $code >= 300) {
            $decoded = json_decode($body, true);
            $message = is_array($decoded)
                ? ($decoded['error'] ?? $decoded['message'] ?? '')
                : '';

            return new WP_Error('ec_rag_api_error', $message ?: ($body ?: 'RAG API error'));
        }

        if ($binary) {
            $content_type = wp_remote_retrieve_header($response, 'content-type') ?: 'audio/mpeg';

            return [
                'contentType' => $content_type,
                'audio'       => base64_encode($body),
            ];
        }

        $decoded = json_decode($body, true);

        return is_array($decoded) ? $decoded : [];
    }
}
