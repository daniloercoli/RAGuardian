<?php
/**
 * EC_Rag_Ajax
 *
 * WordPress AJAX handlers for chat, TTS, and audio upload.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Ajax {

    /**
     * Register AJAX actions.
     *
     * @return void
     */
    public static function register(): void {
        // Public + private handlers.
        add_action('wp_ajax_ec_rag_query', [self::class, 'chat_query']);
        add_action('wp_ajax_nopriv_ec_rag_query', [self::class, 'chat_query']);

        add_action('wp_ajax_ec_rag_tts', [self::class, 'tts']);
        add_action('wp_ajax_nopriv_ec_rag_tts', [self::class, 'tts']);

        add_action('wp_ajax_ec_rag_audio_upload', [self::class, 'audio_upload']);
        add_action('wp_ajax_nopriv_ec_rag_audio_upload', [self::class, 'audio_upload']);
    }

    /**
     * Query endpoint.
     *
     * @return void
     *
     * @wp-hook wp_ajax_ec_rag_chat_query
     */
    public static function chat_query(): void {
        check_ajax_referer('ec_rag_client', 'nonce');

        $options = EC_Rag_Options::get();

        if (!self::is_chat_allowed($options)) {
            wp_send_json_error(['message' => __('Chat is available only to logged-in users', 'ec-rag')], 403);
        }

        $valid = self::check_rate_limit(
            'chat',
            absint($options['rate_limit_requests']),
            absint($options['rate_limit_window'])
        );
        if (is_wp_error($valid)) {
            wp_send_json_error(['message' => $valid->get_error_message()], 429);
        }

        $query = sanitize_textarea_field(wp_unslash($_POST['query'] ?? ''));
        if (strlen($query) < 3) {
            wp_send_json_error(['message' => __('Query is too short', 'ec-rag')], 400);
        }
        if (strlen($query) > 2000) {
            wp_send_json_error(['message' => __('Query is too long', 'ec-rag')], 400);
        }

        $payload = [
            'query'            => $query,
            'conversation_id' => self::conversation_id(),
            'response_language' => EC_Rag_Utils::sanitize_response_language(wp_unslash($_POST['response_language'] ?? $options['response_language'] ?? 'auto')),
        ];

        $client_context = EC_Rag_Utils::client_context($options);
        if ($client_context) {
            $payload['client_context'] = $client_context;
        }

        $api      = new EC_Rag_Api_Client(fn() => $options);
        $response = $api->post('/api/v1/query', $payload);

        if (is_wp_error($response)) {
            wp_send_json_error(['message' => $response->get_error_message()], 502);
        }

        // Source visibility is a server-side policy. Hiding sources only in
        // JavaScript would still expose filenames/snippets in the AJAX body.
        if (($options['show_sources'] ?? '0') !== '1' && is_array($response)) {
            unset($response['sources']);
        }

        wp_send_json_success($response);
    }

    /**
     * TTS endpoint.
     *
     * @return void
     *
     * @wp-hook wp_ajax_ec_rag_tts
     */
    public static function tts(): void {
        check_ajax_referer('ec_rag_client', 'nonce');

        $options = EC_Rag_Options::get();

        if (!self::is_chat_allowed($options)) {
            wp_send_json_error(['message' => __('TTS is available only to logged-in users', 'ec-rag')], 403);
        }

        if ($options['enable_tts'] !== '1') {
            wp_send_json_error(['message' => __('TTS is disabled', 'ec-rag')], 403);
        }

        $valid = self::check_rate_limit(
            'tts',
            absint($options['tts_rate_limit_requests']),
            absint($options['rate_limit_window'])
        );
        if (is_wp_error($valid)) {
            wp_send_json_error(['message' => $valid->get_error_message()], 429);
        }

        $text = sanitize_textarea_field(wp_unslash($_POST['text'] ?? ''));
        if ($text === '') {
            wp_send_json_error(['message' => __('Text is required', 'ec-rag')], 400);
        }
        if (strlen($text) > 2000) {
            wp_send_json_error(['message' => __('Text is too long', 'ec-rag')], 400);
        }

        $api  = new EC_Rag_Api_Client(fn() => $options);
        $data = $api->post('/api/v1/tts', ['text' => $text], true);

        if (is_wp_error($data)) {
            wp_send_json_error(['message' => $data->get_error_message()], 502);
        }

        wp_send_json_success($data);
    }

    /**
     * Audio upload endpoint.
     *
     * @return void
     *
     * @wp-hook wp_ajax_ec_rag_audio_upload
     */
    public static function audio_upload(): void {
        check_ajax_referer('ec_rag_client', 'nonce');

        $options = EC_Rag_Options::get();

        if (!self::is_chat_allowed($options)) {
            wp_send_json_error(['message' => __('Audio upload is available only to logged-in users', 'ec-rag')], 403);
        }

        if ($options['enable_audio_upload'] !== '1') {
            wp_send_json_error(['message' => __('Audio upload is disabled', 'ec-rag')], 403);
        }

        $valid = self::check_rate_limit(
            'audio',
            absint($options['audio_rate_limit_requests']),
            absint($options['rate_limit_window'])
        );
        if (is_wp_error($valid)) {
            wp_send_json_error(['message' => $valid->get_error_message()], 429);
        }

        if (empty($_FILES['audio']) || !is_uploaded_file($_FILES['audio']['tmp_name'])) {
            wp_send_json_error(['message' => __('Audio file is required', 'ec-rag')], 400);
        }

        $file     = $_FILES['audio'];
        $filename = sanitize_file_name($file['name']);
        $ext      = strtolower(pathinfo($filename, PATHINFO_EXTENSION));

        $allowed = ['mp3', 'wav', 'm4a', 'webm', 'ogg', 'flac'];
        if (!$filename || !in_array($ext, $allowed, true)) {
            wp_send_json_error(['message' => __('Unsupported audio format', 'ec-rag')], 400);
        }

        $max = 25 * 1024 * 1024; // 25MB.
        if (filesize($file['tmp_name']) > $max) {
            wp_send_json_error(['message' => __('Audio file is too large', 'ec-rag')], 400);
        }

        $content = file_get_contents($file['tmp_name']);
        if ($content === false || $content === '') {
            wp_send_json_error(['message' => __('Audio file is empty', 'ec-rag')], 400);
        }

        $conv_id     = self::conversation_id();
        $relative_path = 'wordpress/audio/' . sanitize_file_name($conv_id . '-' . time() . '.' . $ext);
        $content_type = $file['type'] ?: 'application/octet-stream';
        $content_type = sanitize_mime_type($content_type);

        $api      = new EC_Rag_Api_Client(fn() => $options);
        $response = $api->post_multipart(
            '/api/v1/audio?async=true',
            ['relative_path' => $relative_path],
            [
                'filename'     => $filename,
                'content_type' => $content_type,
                'content'     => $content,
            ]
        );

        if (is_wp_error($response)) {
            wp_send_json_error(['message' => $response->get_error_message()], 502);
        }

        wp_send_json_success($response);
    }

    // ---------- Helpers ----------

    /**
     * Check if chat is allowed for the current user.
     *
     * @param array $options
     * @return bool
     */
    protected static function is_chat_allowed(array $options): bool {
        return is_user_logged_in() || $options['allow_guest_chat'] === '1';
    }

    /**
     * Check rate limit.
     *
     * @param string $action
     * @param int   $max_requests
     * @param int   $window
     * @return bool|WP_Error
     */
    protected static function check_rate_limit(string $action, int $max_requests, int $window) {
        $limiter = new EC_Rag_Rate_Limiter();

        return $limiter->check($action, $max_requests, $window);
    }

    /**
     * Get the conversation ID for the current request.
     *
     * @return string
     */
    protected static function conversation_id(): string {
        $conv = EC_Rag_Utils::conversation_id_from_request();

        return $conv !== '' ? $conv : 'wp-' . wp_generate_uuid4();
    }
}
