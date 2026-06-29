<?php
/**
 * EC_Rag_Widget
 *
 * Renders the chat widget (floating + inline), shortcode, and asset enqueue.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Widget {

    /**
     * Register hooks.
     *
     * @return void
     */
    public static function register(): void {
        add_action('wp_enqueue_scripts', [self::class, 'maybe_enqueue_assets']);
        add_action('wp_footer', [self::class, 'render_global_widget']);
        add_shortcode('rag_chat', [self::class, 'shortcode']);
    }

    // ---------- Asset Enqueue ----------

    /**
     * Conditionally enqueue frontend assets.
     *
     * @return void
     *
     * @wp-hook wp_enqueue_scripts
     */
    public static function maybe_enqueue_assets(): void {
        if (is_admin()) {
            return;
        }

        $options = EC_Rag_Options::get();

        if (!self::can_current_user_chat($options)) {
            return;
        }

        // Global widget (non-inline).
        if ($options['enable_global_widget'] === '1'
            && $options['position'] !== 'inline-only'
            && !self::is_current_page_excluded($options)) {
            self::enqueue_assets($options);

            return;
        }

        // Inline shortcode.
        $post = get_post();
        if ($post && has_shortcode($post->post_content, 'rag_chat')) {
            self::enqueue_assets($options);
        }
    }

    /**
     * Enqueue JS + CSS assets.
     *
     * @param array $options Plugin options.
     * @return void
     */
    public static function enqueue_assets(array $options): void {
        static $done = false;
        if ($done) {
            return;
        }
        $done = true;

        $plugin_file = defined('EC_RAG_PLUGIN_FILE')
            ? EC_RAG_PLUGIN_FILE
            : dirname(__DIR__) . '/rag-client.php';

        wp_enqueue_style(
            'ec-rag-client',
            plugins_url('assets/rag-client.css', $plugin_file),
            [],
            EC_RAG_VERSION
        );

        wp_enqueue_script(
            'ec-rag-client',
            plugins_url('assets/rag-client.js', $plugin_file),
            [],
            EC_RAG_VERSION,
            true
        );

        // Inject frontend config before the widget script runs.
        $config = [
            'ajaxUrl' => admin_url('admin-ajax.php'),
            'nonce'   => wp_create_nonce('ec_rag_client'),
            'defaults' => [
                'showSources'        => $options['show_sources'] === '1',
                'enableTts'          => $options['enable_tts'] === '1',
                'enableAudioUpload'  => $options['enable_audio_upload'] === '1',
                'responseLanguage'   => $options['response_language'],
            ],
        ];

        wp_add_inline_script(
            'ec-rag-client',
            'window.ecRagClient = ' . wp_json_encode($config) . ';',
            'before'
        );

        // Custom CSS.
        if ($options['appearance_mode'] === 'custom' && !empty($options['custom_css'])) {
            wp_add_inline_style('ec-rag-client', $options['custom_css']);
        }
    }

    // ---------- Shortcode ----------

    /**
     * [rag_chat] shortcode handler.
     *
     * @param array|null $atts
     * @return string
     */
    public static function shortcode(?array $atts = null): string {
        $options = EC_Rag_Options::get();

        if (!self::can_current_user_chat($options)) {
            return '';
        }

        $atts = shortcode_atts(
            [
                'title'             => $options['widget_title'],
                'placeholder'       => $options['input_placeholder'],
                'context'           => '',
                'show_sources'      => $options['show_sources'],
                'enable_tts'        => $options['enable_tts'],
                'response_language' => $options['response_language'],
            ],
            $atts ?? [],
            'rag_chat'
        );

        self::enqueue_assets($options);

        return self::render_chat([
            'mode'               => 'inline',
            'title'              => sanitize_text_field($atts['title']),
            'placeholder'        => sanitize_text_field($atts['placeholder']),
            'context'            => sanitize_textarea_field($atts['context']),
            'show_sources'       => EC_Rag_Utils::is_truthy($atts['show_sources']),
            'enable_tts'         => EC_Rag_Utils::is_truthy($atts['enable_tts']) && $options['enable_tts'] === '1',
            'response_language'  => EC_Rag_Utils::sanitize_response_language($atts['response_language']),
            'options'           => $options,
        ]);
    }

    // ---------- Global Widget ----------

    /**
     * Render the global floating widget in wp_footer.
     *
     * @return void
     *
     * @wp-hook wp_footer
     */
    public static function render_global_widget(): void {
        $options = EC_Rag_Options::get();

        if ($options['enable_global_widget'] !== '1'
            || $options['position'] === 'inline-only'
            || !self::can_current_user_chat($options)
            || self::is_current_page_excluded($options)) {
            return;
        }

        self::enqueue_assets($options);

        echo self::render_chat([
            'mode'               => 'floating',
            'title'              => $options['widget_title'],
            'placeholder'        => $options['input_placeholder'],
            'context'            => '',
            'show_sources'       => $options['show_sources'] === '1',
            'enable_tts'         => $options['enable_tts'] === '1',
            'response_language'  => $options['response_language'],
            'options'           => $options,
        ]);
    }

    // ---------- HTML Rendering ----------

    /**
     * Render the chat widget HTML.
     *
     * @param array $args
     * @return string
     */
    public static function render_chat(array $args): string {
        $options   = $args['options'];
        $mode      = ($args['mode'] === 'floating') ? 'floating' : 'inline';
        $position  = ($mode === 'floating') ? $options['position'] : 'inline-only';
        $id        = 'ec-rag-panel-' . wp_generate_uuid4();
        $primary   = $options['primary_color'] ?: '#2563eb';
        $text_color = $options['text_color'] ?: '#ffffff';
        $widget_title = $args['title'] ?: $options['widget_title'];
        $placeholder = $args['placeholder'] ?: $options['input_placeholder'];

        $style = $options['appearance_mode'] === 'custom'
            ? sprintf('--ec-rag-primary:%s;--ec-rag-on-primary:%s;', esc_attr($primary), esc_attr($text_color))
            : '';

        $page_context = EC_Rag_Utils::page_context();

        $classes = [
            'ec-rag-chat',
            'ec-rag-chat--' . $mode,
            'ec-rag-chat--' . $position,
            'ec-rag-chat--appearance-' . $options['appearance_mode'],
        ];

        ob_start();

        ?>
        <div class="<?php echo esc_attr(implode(' ', $classes)); ?>"
            style="<?php echo esc_attr($style); ?>"
            data-ec-rag-chat
            data-ec-rag-mode="<?php echo esc_attr($mode); ?>"
            data-ec-rag-title="<?php echo esc_attr($widget_title); ?>"
            data-ec-rag-welcome="<?php echo esc_attr($options['welcome_message']); ?>"
            data-ec-rag-context="<?php echo esc_attr($args['context']); ?>"
            data-ec-rag-show-sources="<?php echo $args['show_sources'] ? '1' : '0'; ?>"
            data-ec-rag-enable-tts="<?php echo $args['enable_tts'] ? '1' : '0'; ?>"
            data-ec-rag-response-language="<?php echo esc_attr(EC_Rag_Utils::sanitize_response_language($args['response_language'] ?? 'auto')); ?>"
            data-ec-rag-page-title="<?php echo esc_attr($page_context['page_title']); ?>"
            data-ec-rag-page-url="<?php echo esc_attr($page_context['page_url']); ?>"
            data-ec-rag-post-type="<?php echo esc_attr($page_context['post_type']); ?>"
            data-ec-rag-locale="<?php echo esc_attr($page_context['locale']); ?>">
                <?php if ($mode === 'floating') : ?>
                    <button class="ec-rag-launcher" type="button" data-ec-rag-toggle aria-controls="<?php echo esc_attr($id); ?>" aria-expanded="false">
                        <?php echo self::avatar_markup($options); ?>
                        <span class="ec-rag-launcher__label"><?php echo esc_html($options['launcher_label']); ?></span>
                    </button>
                <?php endif; ?>
                <section id="<?php echo esc_attr($id); ?>" class="ec-rag-panel" data-ec-rag-panel aria-hidden="<?php echo ($mode === 'floating') ? 'true' : 'false'; ?>">
                    <header class="ec-rag-header">
                        <div class="ec-rag-header__identity">
                            <?php echo self::avatar_markup($options); ?>
                            <div class="ec-rag-header__text">
                                <strong><?php echo esc_html($widget_title); ?></strong>
                                <span><?php echo esc_html(!empty($options['assistant_status_label']) ? $options['assistant_status_label'] : 'Online'); ?></span>
                            </div>
                        </div>
                        <div class="ec-rag-header__actions">
                            <button class="ec-rag-header-button ec-rag-download" type="button" data-ec-rag-download aria-label="<?php esc_attr_e('Download conversation', 'ec-rag'); ?>">TXT</button>
                            <?php if ($mode === 'floating') : ?>
                                <button class="ec-rag-header-button ec-rag-close" type="button" data-ec-rag-toggle aria-label="<?php esc_attr_e('Close chat', 'ec-rag'); ?>">&times;</button>
                            <?php endif; ?>
                        </div>
                    </header>
                    <?php if (!empty($options['privacy_note'])) : ?>
                        <p class="ec-rag-privacy-note"><?php echo esc_html($options['privacy_note']); ?></p>
                    <?php endif; ?>
                    <div class="ec-rag-messages" data-ec-rag-messages aria-live="polite"></div>
                    <form class="ec-rag-form" data-ec-rag-form>
                        <?php if ($options['enable_audio_upload'] === '1') : ?>
                            <input class="ec-rag-audio-input" type="file" accept="audio/*" data-ec-rag-audio hidden>
                            <button class="ec-rag-audio-button" type="button" data-ec-rag-audio-button aria-label="<?php esc_attr_e('Upload audio', 'ec-rag'); ?>">Audio</button>
                        <?php endif; ?>
                        <textarea data-ec-rag-input rows="1" placeholder="<?php echo esc_attr($placeholder); ?>"></textarea>
                        <button type="submit" aria-label="<?php esc_attr_e('Send message', 'ec-rag'); ?>"><?php esc_html_e('Send', 'ec-rag'); ?></button>
                    </form>
                </section>
        </div>
        <?php

        return ob_get_clean();
    }

    /**
     * Avatar HTML markup.
     *
     * @param array $options
     * @return string
     */
    protected static function avatar_markup(array $options): string {
        if (!empty($options['avatar_url'])) {
            return '<img class="ec-rag-avatar" src="' . esc_url($options['avatar_url']) . '" alt="" loading="lazy">';
        }

        return '<span class="ec-rag-avatar" aria-hidden="true">EC</span>';
    }

    // ---------- Access checks ----------

    /**
     * Check if the current user can chat.
     *
     * @param array $options
     * @return bool
     */
    public static function can_current_user_chat(array $options): bool {
        return is_user_logged_in() || $options['allow_guest_chat'] === '1';
    }

    /**
     * Check if the current page is excluded.
     *
     * @param array $options
     * @return bool
     */
    public static function is_current_page_excluded(array $options): bool {
        $raw = $options['excluded_pages'] ?? '';
        if (trim($raw) === '') {
            return false;
        }

        $tokens = array_filter(array_map('trim', preg_split('/[\s,]+/', $raw)));
        if (!$tokens) {
            return false;
        }

        $post = get_post();
        $post_id = $post ? (string) $post->ID : '';
        $slug    = $post ? $post->post_name : '';
        $path    = trim(parse_url(wp_unslash($_SERVER['REQUEST_URI'] ?? ''), PHP_URL_PATH) ?: '', '/');

        foreach ($tokens as $token) {
            $normalized = trim($token, '/');
            if ($normalized !== '' && in_array($normalized, [$post_id, $slug, $path], true)) {
                return true;
            }
        }

        return false;
    }
}
