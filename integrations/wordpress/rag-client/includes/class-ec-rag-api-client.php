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
