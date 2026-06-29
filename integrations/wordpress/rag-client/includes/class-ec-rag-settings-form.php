<?php
/**
 * EC_Rag_Settings_Form
 *
 * Renders the admin settings page HTML.
 * Decoupled from EC_Rag_Options for unit-testability.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Settings_Form {

    /**
     * Render the full admin settings page.
     *
     * @return void
     */
    public static function render(): void {
        $options     = EC_Rag_Options::get();
        $import_state = EC_Rag_Ingestion::get_import_state();
        $connection_test = null;

        if (isset($_GET['ec_rag_test']) && current_user_can('manage_options')) {
            check_admin_referer('ec_rag_test_connection');

            $api = new EC_Rag_Api_Client(fn() => $options);
            $connection_test = $api->get('/api/v1/health');
        }
        ?>
        <div class="wrap">
            <h1>Raguardian</h1>
            <p>
                <?php esc_html_e('Connect this WordPress site to one RAGuardian user workspace. The API key stored here decides which user\'s documents, data sources, conversations, and Chroma collection answer the public chatbot.', 'ec-rag'); ?>
            </p>

            <?php if (!empty($_GET['ec_rag_notice'])) : ?>
                <div class="notice notice-success"><p><?php echo esc_html(sanitize_text_field(wp_unslash($_GET['ec_rag_notice']))); ?></p></div>
            <?php endif; ?>

            <?php if (!empty($_GET['ec_rag_error'])) : ?>
                <div class="notice notice-error"><p><?php echo esc_html(sanitize_text_field(wp_unslash($_GET['ec_rag_error']))); ?></p></div>
            <?php endif; ?>

            <?php if ($connection_test !== null) : ?>
                <?php if (is_wp_error($connection_test)) : ?>
                    <div class="notice notice-error"><p><?php echo esc_html($connection_test->get_error_message()); ?></p></div>
                <?php else : ?>
                    <div class="notice notice-success"><p>
                        <?php esc_html_e('RAGuardian connection OK.', 'ec-rag'); ?>
                        <?php printf(esc_html__('Workspace status: %s, documents: %s', 'ec-rag'),
                            esc_html($connection_test['status'] ?? 'unknown'),
                            esc_html((string) ($connection_test['documents_count'] ?? 0))
                        ); ?>
                    </p></div>
                <?php endif; ?>
            <?php endif; ?>

            <form method="post" action="options.php">
                <?php settings_fields('ec_rag_client'); ?>

                <?php self::render_connection_section($options); ?>
                <?php self::render_appearance_section($options); ?>
                <?php self::render_behavior_section($options); ?>
                <?php self::render_ingestion_section($options); ?>
                <?php self::render_rate_limit_section($options); ?>
                <?php self::render_context_section($options); ?>
                <?php self::render_css_section($options); ?>

                <?php submit_button(); ?>
            </form>

            <hr>
            <h2><?php esc_html_e('Initial WordPress export import', 'ec-rag'); ?></h2>
            <p>
                <?php esc_html_e('Go to Tools -> Export in WordPress, export the site, then upload the generated WordPress XML/WXR file here. The plugin extracts only public articles and sends sanitized text snapshots to RAGuardian.', 'ec-rag'); ?>
            </p>
            <?php self::render_import_status($import_state); ?>

            <form method="post" enctype="multipart/form-data" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
                <?php wp_nonce_field('ec_rag_wxr_import'); ?>
                <input type="hidden" name="action" value="ec_rag_wxr_import">
                <input type="file" name="ec_rag_wxr" accept=".xml,.wxr,text/xml,application/xml" required>
                <?php submit_button(__('Upload and queue public articles', 'ec-rag'), 'secondary', 'submit', false); ?>
            </form>

            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" style="margin-top:12px;">
                <?php wp_nonce_field('ec_rag_process_import'); ?>
                <input type="hidden" name="action" value="ec_rag_process_import">
                <?php submit_button(__('Process next batch now', 'ec-rag'), 'secondary', 'submit', false); ?>
            </form>

            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" style="margin-top:12px;">
                <?php wp_nonce_field('ec_rag_clear_import'); ?>
                <input type="hidden" name="action" value="ec_rag_clear_import">
                <?php submit_button(__('Clear import queue', 'ec-rag'), 'delete', 'submit', false); ?>
            </form>
        </div>
        <?php
    }

    /**
     * Connection settings section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_connection_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Connection', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-base-url'); ?>"><?php esc_html_e('RAG Base URL', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-base-url'); ?>"
                        class="regular-text"
                        type="url"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[base_url]"
                        value="<?php echo esc_attr($options['base_url'] ?? ''); ?>"
                        placeholder="https://rag.example.com">
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-api-key'); ?>"><?php esc_html_e('API Key', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-api-key'); ?>"
                        class="regular-text"
                        type="password"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[api_key]"
                        value="<?php echo esc_attr($options['api_key'] ?? ''); ?>">
                    <p class="description">
                        <?php esc_html_e('Required scopes: <code>query</code> for chat, <code>ingest</code> for article import/sync, <code>speech</code> for TTS or audio.', 'ec-rag'); ?>
                    </p>
                </td>
            </tr>
            <tr>
                <th scope="row"><?php esc_html_e('Connection test', 'ec-rag'); ?></th>
                <td>
                    <a class="button" href="<?php echo esc_url(wp_nonce_url(admin_url('options-general.php?page=ec-rag-client&ec_rag_test=1'), 'ec_rag_test_connection')); ?>">
                        <?php esc_html_e('Test RAGuardian health', 'ec-rag'); ?>
                    </a>
                    <p class="description"><?php esc_html_e('Uses the saved Base URL and API key to call /api/v1/health.', 'ec-rag'); ?></p>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-request-timeout'); ?>"><?php esc_html_e('Request timeout', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-request-timeout'); ?>"
                        class="small-text"
                        type="number"
                        min="5"
                        max="120"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[request_timeout]"
                        value="<?php echo esc_attr($options['request_timeout'] ?? '45'); ?>">
                    <span class="description"><?php esc_html_e('seconds', 'ec-rag'); ?></span>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Appearance settings section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_appearance_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Appearance', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-appearance-mode'); ?>"><?php esc_html_e('Style mode', 'ec-rag'); ?></label></th>
                <td>
                    <select id="<?php echo esc_attr('ec-rag-appearance-mode'); ?>"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[appearance_mode]">
                        <option value="theme" <?php selected($options['appearance_mode'], 'theme'); ?>><?php esc_html_e('Use current theme styles', 'ec-rag'); ?></option>
                        <option value="custom" <?php selected($options['appearance_mode'], 'custom'); ?>><?php esc_html_e('Use custom colors/CSS', 'ec-rag'); ?></option>
                    </select>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-widget-title'); ?>"><?php esc_html_e('Widget title', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-widget-title'); ?>"
                        class="regular-text"
                        type="text"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[widget_title]"
                        value="<?php echo esc_attr($options['widget_title'] ?? ''); ?>">
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-welcome-message'); ?>"><?php esc_html_e('Welcome message', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-welcome-message'); ?>"
                        class="regular-text"
                        type="text"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[welcome_message]"
                        value="<?php echo esc_attr($options['welcome_message'] ?? ''); ?>">
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-input-placeholder'); ?>"><?php esc_html_e('Input placeholder', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-input-placeholder'); ?>"
                        class="regular-text"
                        type="text"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[input_placeholder]"
                        value="<?php echo esc_attr($options['input_placeholder'] ?? ''); ?>">
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-launcher-label'); ?>"><?php esc_html_e('Launcher label', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-launcher-label'); ?>"
                        class="regular-text"
                        type="text"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[launcher_label]"
                        value="<?php echo esc_attr($options['launcher_label'] ?? ''); ?>">
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-status-label'); ?>"><?php esc_html_e('Status label', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-status-label'); ?>"
                        class="regular-text"
                        type="text"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[assistant_status_label]"
                        value="<?php echo esc_attr($options['assistant_status_label'] ?? ''); ?>">
                </td>
            </tr>
            <tr>
                <th scope="row"><?php esc_html_e('Colors', 'ec-rag'); ?></th>
                <td>
                    <label><?php esc_html_e('Primary', 'ec-rag'); ?> <input type="color"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[primary_color]"
                        value="<?php echo esc_attr($options['primary_color'] ?? '#2563eb'); ?>"></label>
                    <label style="margin-left:16px;"><?php esc_html_e('Text', 'ec-rag'); ?> <input type="color"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[text_color]"
                        value="<?php echo esc_attr($options['text_color'] ?? '#ffffff'); ?>"></label>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-avatar-url'); ?>"><?php esc_html_e('Avatar or logo URL', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-avatar-url'); ?>"
                        class="regular-text"
                        type="url"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[avatar_url]"
                        value="<?php echo esc_attr($options['avatar_url'] ?? ''); ?>"
                        placeholder="https://example.com/logo.png">
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Behavior settings section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_behavior_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Behavior', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><?php esc_html_e('Global widget', 'ec-rag'); ?></th>
                <td>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[enable_global_widget]"
                        value="1" <?php checked($options['enable_global_widget'], '1'); ?>>
                        <?php esc_html_e('Enable on all pages', 'ec-rag'); ?></label>
                </td>
            </tr>
            <tr>
                <th scope="row"><?php esc_html_e('Guest visibility', 'ec-rag'); ?></th>
                <td>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[allow_guest_chat]"
                        value="1" <?php checked($options['allow_guest_chat'], '1'); ?>>
                        <?php esc_html_e('Show and allow the chatbot for visitors who are not logged in', 'ec-rag'); ?></label>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-position'); ?>"><?php esc_html_e('Position', 'ec-rag'); ?></label></th>
                <td>
                    <select id="<?php echo esc_attr('ec-rag-position'); ?>"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[position]">
                        <option value="bottom-right" <?php selected($options['position'], 'bottom-right'); ?>><?php esc_html_e('Bottom right', 'ec-rag'); ?></option>
                        <option value="bottom-left" <?php selected($options['position'], 'bottom-left'); ?>><?php esc_html_e('Bottom left', 'ec-rag'); ?></option>
                        <option value="inline-only" <?php selected($options['position'], 'inline-only'); ?>><?php esc_html_e('Inline only', 'ec-rag'); ?></option>
                    </select>
                </td>
            </tr>
            <tr>
                <th scope="row"><?php esc_html_e('Chat features', 'ec-rag'); ?></th>
                <td>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[show_sources]"
                        value="1" <?php checked($options['show_sources'], '1'); ?> /> <?php esc_html_e('Show sources', 'ec-rag'); ?></label><br>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[enable_tts]"
                        value="1" <?php checked($options['enable_tts'], '1'); ?> /> <?php esc_html_e('Enable text-to-speech button', 'ec-rag'); ?></label><br>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[enable_audio_upload]"
                        value="1" <?php checked($options['enable_audio_upload'], '1'); ?> /> <?php esc_html_e('Enable audio upload and transcription', 'ec-rag'); ?></label>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-response-language'); ?>"><?php esc_html_e('Response language', 'ec-rag'); ?></label></th>
                <td>
                    <select id="<?php echo esc_attr('ec-rag-response-language'); ?>"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[response_language]">
                        <option value="auto" <?php selected($options['response_language'], 'auto'); ?>><?php esc_html_e('Auto: answer in the visitor question language', 'ec-rag'); ?></option>
                        <option value="it" <?php selected($options['response_language'], 'it'); ?>><?php esc_html_e('Italian', 'ec-rag'); ?></option>
                        <option value="en" <?php selected($options['response_language'], 'en'); ?>><?php esc_html_e('English', 'ec-rag'); ?></option>
                    </select>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-excluded-pages'); ?>"><?php esc_html_e('Excluded pages/posts', 'ec-rag'); ?></label></th>
                <td>
                    <textarea id="<?php echo esc_attr('ec-rag-excluded-pages'); ?>"
                        class="large-text code"
                        rows="3"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[excluded_pages]"
                        placeholder="123, pricing, /private-area"><?php echo esc_textarea($options['excluded_pages'] ?? ''); ?></textarea>
                    <p class="description"><?php esc_html_e('Use IDs, slugs, or URL paths separated by commas or new lines.', 'ec-rag'); ?></p>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Ingestion settings section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_ingestion_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Article ingestion', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><?php esc_html_e('Live sync', 'ec-rag'); ?></th>
                <td>
                    <label><input type="checkbox"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[ingest_public_posts]"
                        value="1" <?php checked($options['ingest_public_posts'], '1'); ?> />
                        <?php esc_html_e('Keep public articles synchronized with RAGuardian', 'ec-rag'); ?></label>
                    <p class="description"><?php esc_html_e('Uses WordPress hooks for newly published, updated, unpublished, or deleted public posts. Password-protected posts are ignored.', 'ec-rag'); ?></p>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-ingestion-batch-size'); ?>"><?php esc_html_e('Import batch size', 'ec-rag'); ?></label></th>
                <td>
                    <input id="<?php echo esc_attr('ec-rag-ingestion-batch-size'); ?>"
                        class="small-text"
                        type="number"
                        min="1"
                        max="50"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[ingestion_batch_size]"
                        value="<?php echo esc_attr($options['ingestion_batch_size'] ?? '10'); ?>">
                    <span class="description"><?php esc_html_e('articles per WordPress cron batch', 'ec-rag'); ?></span>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Rate limit settings section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_rate_limit_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Abuse prevention', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><?php esc_html_e('Chat rate limit', 'ec-rag'); ?></th>
                <td>
                    <input class="small-text" type="number"
                        min="1" max="120"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[rate_limit_requests]"
                        value="<?php echo esc_attr($options['rate_limit_requests'] ?? '10'); ?>">
                    <?php esc_html_e('requests every', 'ec-rag'); ?>
                    <input class="small-text" type="number"
                        min="10" max="3600"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[rate_limit_window]"
                        value="<?php echo esc_attr($options['rate_limit_window'] ?? '60'); ?>">
                    <?php esc_html_e('seconds', 'ec-rag'); ?>
                </td>
            </tr>
            <tr>
                <th scope="row"><?php esc_html_e('Audio limits', 'ec-rag'); ?></th>
                <td>
                    <label>TTS <input class="small-text" type="number"
                        min="1" max="60"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[tts_rate_limit_requests]"
                        value="<?php echo esc_attr($options['tts_rate_limit_requests'] ?? '5'); ?>"></label>
                    <label style="margin-left:16px;">
                        <?php esc_html_e('Audio upload', 'ec-rag'); ?>
                        <input class="small-text" type="number"
                            min="1" max="20"
                            name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[audio_rate_limit_requests]"
                            value="<?php echo esc_attr($options['audio_rate_limit_requests'] ?? '2'); ?>">
                    </label>
                    <span class="description"><?php esc_html_e('requests per rate-limit window', 'ec-rag'); ?></span>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Context section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_context_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Context', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-global-context'); ?>"><?php esc_html_e('Global prompt context', 'ec-rag'); ?></label></th>
                <td>
                    <textarea id="<?php echo esc_attr('ec-rag-global-context'); ?>"
                        class="large-text"
                        rows="4"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[global_context]"
                        placeholder="<?php esc_attr_e('Example: The visitor is browsing the company website. Keep answers concise and commercial.', 'ec-rag'); ?>"><?php echo esc_textarea($options['global_context'] ?? ''); ?></textarea>
                </td>
            </tr>
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-privacy-note'); ?>"><?php esc_html_e('Privacy note', 'ec-rag'); ?></label></th>
                <td>
                    <textarea id="<?php echo esc_attr('ec-rag-privacy-note'); ?>"
                        class="large-text"
                        rows="3"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[privacy_note]"
                        placeholder="<?php esc_attr_e('Example: The assistant uses only the public website knowledge base. Do not enter personal data.', 'ec-rag'); ?>"><?php echo esc_textarea($options['privacy_note'] ?? ''); ?></textarea>
                    <p class="description"><?php esc_html_e('Displayed as a small note inside the chat panel. It is not sent to RAGuardian.', 'ec-rag'); ?></p>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * CSS section.
     *
     * @param array $options
     * @return void
     */
    protected static function render_css_section(array $options): void {
        ?>
        <h2><?php esc_html_e('Advanced CSS', 'ec-rag'); ?></h2>
        <table class="form-table" role="presentation">
            <tr>
                <th scope="row"><label for="<?php echo esc_attr('ec-rag-custom-css'); ?>"><?php esc_html_e('Custom CSS', 'ec-rag'); ?></label></th>
                <td>
                    <textarea id="<?php echo esc_attr('ec-rag-custom-css'); ?>"
                        class="large-text code"
                        rows="6"
                        name="<?php echo esc_attr(EC_Rag_Options::OPTION_NAME); ?>[custom_css]"
                        placeholder=".ec-rag-widget { bottom: 32px; }"><?php echo esc_textarea($options['custom_css'] ?? ''); ?></textarea>
                </td>
            </tr>
        </table>
        <?php
    }

    /**
     * Import status table.
     *
     * @param array|null $state
     * @return void
     */
    protected static function render_import_status(?array $state = null): void {
        if (!$state) {
            echo '<p><em>' . esc_html__('No import queue yet.', 'ec-rag') . '</em></p>';

            return;
        }

        $status    = $state['status'] ?? 'unknown';
        $total     = absint($state['total'] ?? 0);
        $processed = absint($state['processed'] ?? 0);
        $succeeded = absint($state['succeeded'] ?? 0);
        $failed    = absint($state['failed'] ?? 0);
        $created   = sanitize_text_field($state['created_at'] ?? '');

        echo '<table class="widefat striped" style="max-width:760px;"><tbody>';
        echo '<tr><th>' . esc_html__('Status', 'ec-rag') . '</th><td>' . esc_html($status) . '</td></tr>';
        echo '<tr><th>' . esc_html__('Created', 'ec-rag') . '</th><td>' . esc_html($created) . '</td></tr>';
        echo '<tr><th>' . esc_html__('Articles', 'ec-rag') . '</th><td>';
        echo esc_html(
            sprintf(
                __('%1$s / %2$s processed, %3$s accepted, %4$s failed', 'ec-rag'),
                number_format_i18n($processed),
                number_format_i18n($total),
                number_format_i18n($succeeded),
                number_format_i18n($failed)
            )
        );
        echo '</td></tr>';
        echo '</tbody></table>';

        if (!empty($state['errors']) && is_array($state['errors'])) {
            echo '<p><strong>' . esc_html__('Latest errors', 'ec-rag') . '</strong></p><ul>';
            foreach (array_slice($state['errors'], -5) as $error) {
                echo '<li>' . esc_html($error) . '</li>';
            }
            echo '</ul>';
        }
    }
}
