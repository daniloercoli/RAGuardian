<?php
/**
 * EC_Rag_Options
 *
 * WordPress Settings API integration.
 * Handles registration, sanitization, and admin UI.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Options {

    const OPTION_NAME = 'ec_rag_client_options';

    /**
     * Register the settings with WordPress.
     *
     * @return void
     */
    public static function register(): void {
        add_action('admin_init', [self::class, 'register_settings']);
        add_action('admin_menu', [self::class, 'admin_menu']);
        add_action('admin_enqueue_scripts', [self::class, 'enqueue_admin_assets']);
    }

    /** Enqueue Agent CRUD behavior only on this plugin's settings page. */
    public static function enqueue_admin_assets(string $hook): void {
        if ($hook !== 'settings_page_ec-rag-client') {
            return;
        }
        wp_enqueue_script(
            'ec-rag-admin-agents',
            plugins_url('assets/rag-admin-agents.js', EC_RAG_PLUGIN_FILE),
            [],
            EC_RAG_VERSION,
            true
        );
    }

    /**
     * Register the settings API.
     *
     * @return void
     */
    public static function register_settings(): void {
        register_setting('ec_rag_client', self::OPTION_NAME, [self::class, 'sanitize']);
    }

    /**
     * Add the admin menu page (Settings -> Raguardian).
     *
     * @return void
     */
    public static function admin_menu(): void {
        add_options_page(
            __('Raguardian', 'ec-rag'),
            __('Raguardian', 'ec-rag'),
            'manage_options',
            'ec-rag-client',
            [self::class, 'render_settings_page']
        );
    }

    /**
     * Get the plugin options.
     *
     * @return array
     */
    public static function get(): array {
        $defaults  = self::defaults();
        $saved     = get_option(self::OPTION_NAME, []);
        $merged    = is_array($saved) ? $saved : [];
        $options   = wp_parse_args($merged, $defaults);

        // Read-time migration keeps old installations working immediately;
        // the normalized values are persisted the next time settings are saved.
        $legacy_agent_id = self::sanitize_agent_id($options['agent_id'] ?? '');
        if (empty($options['allowed_agent_ids']) && $legacy_agent_id !== '') {
            $options['allowed_agent_ids'] = [$legacy_agent_id];
        }
        if (empty($options['default_agent_id']) && $legacy_agent_id !== '') {
            $options['default_agent_id'] = $legacy_agent_id;
        }
        if (!array_key_exists('ingestion_knowledge_base_id', $merged)) {
            $options['ingestion_knowledge_base_id'] = $options['knowledge_base_id'] ?? '';
        }
        $options['allowed_agent_ids'] = self::sanitize_agent_ids(
            $options['allowed_agent_ids'] ?? []
        );
        $options['default_agent_id'] = self::sanitize_agent_id(
            $options['default_agent_id'] ?? ''
        );
        if (!in_array($options['default_agent_id'], $options['allowed_agent_ids'], true)) {
            $options['default_agent_id'] = '';
        }
        // Keep the legacy alias for third-party code until a later major version.
        $options['agent_id'] = $options['default_agent_id'];

        return $options;
    }

    /**
     * Default option values.
     *
     * @return array
     */
    public static function defaults(): array {
        return [
            'base_url'                => '',
            'api_key'                => '',
            'knowledge_base_id'      => '',
            'agent_id'               => '',
            'allowed_agent_ids'      => [],
            'default_agent_id'       => '',
            'ingestion_knowledge_base_id' => '',
            'response_language'       => 'auto',
            'request_timeout'        => '45',
            'show_sources'           => '1',
            'enable_tts'             => '0',
            'enable_audio_upload'    => '0',
            'enable_global_widget'   => '0',
            'allow_guest_chat'       => '0',
            'ingest_public_posts'    => '0',
            'ingestion_batch_size'   => '10',
            'appearance_mode'        => 'theme',
            'position'               => 'bottom-right',
            'widget_title'           => __('Ask our assistant', 'ec-rag'),
            'welcome_message'        => __('Hi, how can I help?', 'ec-rag'),
            'input_placeholder'      => __('Ask a question', 'ec-rag'),
            'launcher_label'         => __('Chat', 'ec-rag'),
            'assistant_status_label' => __('Online', 'ec-rag'),
            'primary_color'         => '#2563eb',
            'text_color'            => '#ffffff',
            'avatar_url'            => '',
            'global_context'        => '',
            'privacy_note'          => '',
            'excluded_pages'        => '',
            'custom_css'            => '',
            'rate_limit_requests'   => '10',
            'rate_limit_window'     => '60',
            'tts_rate_limit_requests' => '5',
            'audio_rate_limit_requests' => '2',
            'enable_chat_agents_mode' => '0',
        ];
    }

    /**
     * Sanitize the full options array.
     *
     * @param array $input Raw input from the settings form.
     * @return array
     */
    public static function sanitize(array $input): array {
        $input = $input ?? [];

        $position = sanitize_key($input['position'] ?? 'bottom-right');
        $position = in_array($position, ['bottom-right', 'bottom-left', 'inline-only'], true)
            ? $position
            : 'bottom-right';

        $appearance_mode = sanitize_key($input['appearance_mode'] ?? 'theme');
        $appearance_mode = in_array($appearance_mode, ['theme', 'custom'], true)
            ? $appearance_mode
            : 'theme';

        $primary = $input['primary_color'] ?? '#2563eb';
        $primary = sanitize_hex_color($primary) ?: '#2563eb';

        $text = $input['text_color'] ?? '#ffffff';
        $text = sanitize_hex_color($text) ?: '#ffffff';

        $timeout = absint($input['request_timeout'] ?? 45);
        $timeout = ($timeout >= 5 && $timeout <= 120) ? $timeout : 45;

        $ingest_batch = absint($input['ingestion_batch_size'] ?? 10);
        $ingest_batch = ($ingest_batch >= 1 && $ingest_batch <= 50) ? $ingest_batch : 10;

        $rate_limit = absint($input['rate_limit_requests'] ?? 10);
        $rate_limit = ($rate_limit >= 1 && $rate_limit <= 120) ? $rate_limit : 10;

        $rate_window = absint($input['rate_limit_window'] ?? 60);
        $rate_window = ($rate_window >= 10 && $rate_window <= 3600) ? $rate_window : 60;

        $tts_limit = absint($input['tts_rate_limit_requests'] ?? 5);
        $tts_limit = ($tts_limit >= 1 && $tts_limit <= 60) ? $tts_limit : 5;

        $audio_limit = absint($input['audio_rate_limit_requests'] ?? 2);
        $audio_limit = ($audio_limit >= 1 && $audio_limit <= 20) ? $audio_limit : 2;
        $knowledge_base_id = sanitize_text_field($input['knowledge_base_id'] ?? '');
        if ($knowledge_base_id === 'default') {
            $knowledge_base_id = '';
        }
        $ingestion_knowledge_base_id = self::sanitize_knowledge_base_id(
            $input['ingestion_knowledge_base_id'] ?? $knowledge_base_id
        );
        $allowed_agent_ids = self::sanitize_agent_ids(
            $input['allowed_agent_ids'] ?? []
        );
        $default_agent_id = self::sanitize_agent_id(
            $input['default_agent_id'] ?? ($input['agent_id'] ?? '')
        );
        if (!in_array($default_agent_id, $allowed_agent_ids, true)) {
            $default_agent_id = '';
        }
        if ($knowledge_base_id !== '' && !preg_match('/^kb_[0-9a-f]{32}$/', $knowledge_base_id)) {
            $knowledge_base_id = '';
        }

        return [
            'base_url'                => esc_url_raw(rtrim($input['base_url'] ?? '', '/')),
            'api_key'                => sanitize_text_field($input['api_key'] ?? ''),
            'knowledge_base_id'      => $knowledge_base_id,
            'ingestion_knowledge_base_id' => $ingestion_knowledge_base_id,
            'response_language'       => EC_Rag_Utils::sanitize_response_language($input['response_language'] ?? 'auto'),
            'request_timeout'        => (string) $timeout,
            'show_sources'           => !empty($input['show_sources']) ? '1' : '0',
            'enable_tts'             => !empty($input['enable_tts']) ? '1' : '0',
            'enable_audio_upload'    => !empty($input['enable_audio_upload']) ? '1' : '0',
            'enable_global_widget'   => !empty($input['enable_global_widget']) ? '1' : '0',
            'allow_guest_chat'       => !empty($input['allow_guest_chat']) ? '1' : '0',
            'ingest_public_posts'    => !empty($input['ingest_public_posts']) ? '1' : '0',
            'ingestion_batch_size'   => (string) $ingest_batch,
            'appearance_mode'        => $appearance_mode,
            'position'               => $position,
            'widget_title'           => sanitize_text_field($input['widget_title'] ?? ''),
            'welcome_message'        => sanitize_text_field($input['welcome_message'] ?? ''),
            'input_placeholder'      => sanitize_text_field($input['input_placeholder'] ?? ''),
            'launcher_label'         => sanitize_text_field($input['launcher_label'] ?? ''),
            'assistant_status_label' => sanitize_text_field($input['assistant_status_label'] ?? ''),
            'primary_color'         => $primary,
            'text_color'            => $text,
            'avatar_url'            => esc_url_raw($input['avatar_url'] ?? ''),
            'global_context'        => sanitize_textarea_field($input['global_context'] ?? ''),
            'privacy_note'          => sanitize_textarea_field($input['privacy_note'] ?? ''),
            'excluded_pages'        => sanitize_textarea_field($input['excluded_pages'] ?? ''),
            'custom_css'            => trim(wp_strip_all_tags($input['custom_css'] ?? '')),
            'rate_limit_requests'   => (string) $rate_limit,
            'rate_limit_window'     => (string) $rate_window,
            'tts_rate_limit_requests' => (string) $tts_limit,
            'audio_rate_limit_requests' => (string) $audio_limit,
            'enable_chat_agents_mode' => !empty($input['enable_chat_agents_mode']) ? '1' : '0',
            'allowed_agent_ids'      => $allowed_agent_ids,
            'default_agent_id'       => $default_agent_id,
            'agent_id'               => $default_agent_id,
        ];
    }

    /**
     * Sanitize one Agent identifier.
     */
    public static function sanitize_agent_id($value): string {
        $value = sanitize_text_field((string) $value);
        return preg_match('/^agent_[0-9a-f]{32}$/', $value) ? $value : '';
    }

    /**
     * Sanitize and deduplicate an Agent allowlist without changing its order.
     */
    public static function sanitize_agent_ids($values): array {
        if (!is_array($values)) {
            return [];
        }
        $result = [];
        foreach ($values as $value) {
            $agent_id = self::sanitize_agent_id($value);
            if ($agent_id !== '' && !in_array($agent_id, $result, true)) {
                $result[] = $agent_id;
            }
        }
        return $result;
    }

    /**
     * Sanitize a KB target. Empty and "default" both mean the default KB.
     */
    public static function sanitize_knowledge_base_id($value): string {
        $value = sanitize_text_field((string) $value);
        if ($value === '' || $value === 'default') {
            return '';
        }
        return preg_match('/^kb_[0-9a-f]{32}$/', $value) ? $value : '';
    }

    /**
     * Render the admin settings page (HTML).
     *
     * @return void
     */
    public static function render_settings_page(): void {
        EC_Rag_Settings_Form::render();
    }
}
