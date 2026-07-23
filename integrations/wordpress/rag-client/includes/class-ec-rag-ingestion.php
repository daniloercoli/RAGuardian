<?php
/**
 * EC_Rag_Ingestion
 *
 * Handles WordPress post live sync and WXR import.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Ingestion {

    const IMPORT_OPTION = 'ec_rag_client_import_state';
    const CRON_HOOK     = 'ec_rag_process_import_batch';
    const POST_SYNC_HOOK = 'ec_rag_sync_post';
    const POST_SYNC_MAX_RETRIES = 3;

    /**
     * Register WordPress hooks for live sync.
     *
     * @return void
     */
    public static function register(): void {
        add_action('admin_post_ec_rag_wxr_import', [self::class, 'handle_wxr_import_upload']);
        add_action('admin_post_ec_rag_process_import', [self::class, 'handle_process_import']);
        add_action('admin_post_ec_rag_clear_import', [self::class, 'handle_clear_import']);
        add_action('transition_post_status', [self::class, 'on_transition_post_status'], 10, 3);
        add_action('save_post_post', [self::class, 'on_save_post'], 20, 3);
        add_action('before_delete_post', [self::class, 'on_delete_post'], 10, 2);
        add_action(self::CRON_HOOK, [self::class, 'cron_process_import_batch']);
        add_action(self::POST_SYNC_HOOK, [self::class, 'cron_sync_post'], 10, 2);
    }

    /**
     * Clear plugin-owned cron jobs on deactivation.
     *
     * @return void
     */
    public static function deactivate(): void {
        wp_unschedule_hook(self::CRON_HOOK);
        wp_unschedule_hook(self::POST_SYNC_HOOK);
    }

    /**
     * Redirect back to the plugin settings page with query args.
     *
     * @param array $args Query args.
     * @return void
     */
    protected static function admin_redirect(array $args): void {
        wp_safe_redirect(add_query_arg($args, admin_url('options-general.php?page=ec-rag-client')));
        exit;
    }

    /**
     * Handle initial WXR upload from the admin settings page.
     *
     * @return void
     *
     * @wp-hook admin_post_ec_rag_wxr_import
     */
    public static function handle_wxr_import_upload(): void {
        if (!current_user_can('manage_options')) {
            wp_die(__('Forbidden', 'ec-rag'), '', ['response' => 403]);
        }

        check_admin_referer('ec_rag_wxr_import');

        if (empty($_FILES['ec_rag_wxr'])) {
            self::admin_redirect(['ec_rag_error' => __('Upload a WordPress export file first.', 'ec-rag')]);
        }

        $upload_name = isset($_FILES['ec_rag_wxr']['name'])
            ? sanitize_file_name(wp_unslash($_FILES['ec_rag_wxr']['name']))
            : '';
        $extension = strtolower(pathinfo($upload_name, PATHINFO_EXTENSION));

        if (!in_array($extension, ['xml', 'wxr'], true)) {
            self::admin_redirect(['ec_rag_error' => __('Upload a WordPress XML/WXR export file.', 'ec-rag')]);
        }

        require_once ABSPATH . 'wp-admin/includes/file.php';

        $uploaded = wp_handle_upload($_FILES['ec_rag_wxr'], [
            'test_form' => false,
            'test_type' => false,
            'mimes'     => [
                'xml' => 'text/xml',
                'wxr' => 'text/xml',
            ],
        ]);

        if (!empty($uploaded['error'])) {
            self::admin_redirect(['ec_rag_error' => $uploaded['error']]);
        }

        $result = self::prepare_wxr_queue($uploaded['file']);
        wp_delete_file($uploaded['file']);

        if (is_wp_error($result)) {
            self::admin_redirect(['ec_rag_error' => $result->get_error_message()]);
        }

        self::schedule_import();
        self::admin_redirect([
            'ec_rag_notice' => sprintf(
                __('WordPress export queued: %d public articles found.', 'ec-rag'),
                absint($result['total'] ?? 0)
            ),
        ]);
    }

    /**
     * Process one import batch from the admin settings page.
     *
     * @return void
     *
     * @wp-hook admin_post_ec_rag_process_import
     */
    public static function handle_process_import(): void {
        if (!current_user_can('manage_options')) {
            wp_die(__('Forbidden', 'ec-rag'), '', ['response' => 403]);
        }

        check_admin_referer('ec_rag_process_import');

        $result = self::process_import_batch();
        if (is_wp_error($result)) {
            self::admin_redirect(['ec_rag_error' => $result->get_error_message()]);
        }

        self::admin_redirect(['ec_rag_notice' => __('Import batch processed.', 'ec-rag')]);
    }

    /**
     * Clear the current import queue from the admin settings page.
     *
     * @return void
     *
     * @wp-hook admin_post_ec_rag_clear_import
     */
    public static function handle_clear_import(): void {
        if (!current_user_can('manage_options')) {
            wp_die(__('Forbidden', 'ec-rag'), '', ['response' => 403]);
        }

        check_admin_referer('ec_rag_clear_import');

        $state = self::get_import_state();
        if (!empty($state['queue_path'])) {
            wp_delete_file($state['queue_path']);
        }

        wp_clear_scheduled_hook(self::CRON_HOOK);
        delete_option(self::IMPORT_OPTION);
        self::admin_redirect(['ec_rag_notice' => __('Import queue cleared.', 'ec-rag')]);
    }

    /**
     * Check if live ingestion is enabled.
     *
     * @return bool
     */
    public static function is_enabled(): bool {
        $opts = EC_Rag_Options::get();

        return $opts['ingest_public_posts'] === '1';
    }

    /**
     * Hook: transition_post_status.
     *
     * @param string    $new_status
     * @param string    $old_status
     * @param WP_Post   $post
     * @return void
     */
    public static function on_transition_post_status(string $new_status, string $old_status, $post): void {
        if (!self::is_enabled()) {
            return;
        }

        if (wp_is_post_revision($post->ID) || wp_is_post_autosave($post->ID)) {
            return;
        }

        if ($post->post_type !== 'post') {
            return;
        }

        if (EC_Rag_Utils::is_public_article($post)) {
            self::schedule_post_sync($post->ID);
            self::mark_handled($post->ID);

            return;
        }

        if ($old_status === 'publish') {
            self::schedule_post_sync($post->ID);
            self::mark_handled($post->ID);
        }
    }

    /**
     * Hook: save_post_post.
     *
     * @param int       $post_id
     * @param WP_Post   $post
     * @param bool      $update
     * @return void
     */
    public static function on_save_post(int $post_id, $post, bool $update): void {
        if (!self::is_enabled()) {
            return;
        }

        if (wp_is_post_revision($post_id) || wp_is_post_autosave($post_id)) {
            return;
        }

        if (self::was_handled($post_id)) {
            return;
        }

        if (EC_Rag_Utils::is_public_article($post)) {
            self::schedule_post_sync($post_id);
        }
    }

    /**
     * Hook: before_delete_post.
     *
     * @param int       $post_id
     * @param WP_Post   $post
     * @return void
     */
    public static function on_delete_post(int $post_id, $post): void {
        if (!self::is_enabled() || !$post || $post->post_type !== 'post') {
            return;
        }

        self::schedule_post_sync($post_id);
    }

    /**
     * Queue a post synchronization outside the editorial request.
     *
     * @param int $post_id Post ID.
     * @param int $attempt Retry attempt.
     * @param int $delay Delay in seconds.
     * @return void
     */
    public static function schedule_post_sync(int $post_id, int $attempt = 0, int $delay = 1): void {
        $post_id = absint($post_id);
        $attempt = absint($attempt);
        $args     = [$post_id, $attempt];

        if (!$post_id) {
            return;
        }

        if ($attempt === 0) {
            for ($retry = 1; $retry <= self::POST_SYNC_MAX_RETRIES; $retry++) {
                wp_clear_scheduled_hook(self::POST_SYNC_HOOK, [$post_id, $retry]);
            }
        }

        if (wp_next_scheduled(self::POST_SYNC_HOOK, $args)) {
            return;
        }

        wp_schedule_single_event(time() + max(1, $delay), self::POST_SYNC_HOOK, $args);
    }

    /**
     * Synchronize the latest post state from WP-Cron.
     *
     * @param int $post_id Post ID.
     * @param int $attempt Retry attempt.
     * @return void
     */
    public static function cron_sync_post(int $post_id, int $attempt = 0): void {
        $post = get_post($post_id);

        if ($post && EC_Rag_Utils::is_public_article($post)) {
            if (!self::is_enabled()) {
                return;
            }

            $response = self::ingest_article(EC_Rag_Utils::article_from_post($post));
            $operation = 'upload';
        } else {
            $response = self::delete_snapshot($post_id);
            $operation = 'delete';
        }

        if (!is_wp_error($response)) {
            return;
        }

        EC_Rag_Logger::log(
            sprintf(
                'Post %d %s failed on background attempt %d: %s',
                $post_id,
                $operation,
                $attempt + 1,
                $response->get_error_message()
            ),
            'post_sync',
            2
        );

        if ($attempt < self::POST_SYNC_MAX_RETRIES) {
            $retry_delay = min(HOUR_IN_SECONDS, MINUTE_IN_SECONDS * (2 ** $attempt));
            self::schedule_post_sync($post_id, $attempt + 1, $retry_delay);
        }
    }

    /**
     * Sync a single post to RAGuardian.
     *
     * @param WP_Post $post The post.
     * @return void
     */
    public static function sync_post($post): void {
        $article = EC_Rag_Utils::article_from_post($post);
        self::ingest_article($article);
    }

    /**
     * Ingest a single article snapshot.
     *
     * @param array $article The article data.
     * @return array|WP_Error|null
     */
    public static function ingest_article(array $article) {
        $post_id = absint($article['post_id'] ?? 0);

        if (!$post_id) {
            return new WP_Error('ec_rag_missing_post_id', __('Article is missing a post id.', 'ec-rag'));
        }

        $filename     = 'post-' . $post_id . '.txt';
        $relative_path = 'wordpress/posts/' . $filename;
        $content     = EC_Rag_Utils::snapshot_content($article);

        return self::upload_snapshot($filename, $relative_path, $content);
    }

    /**
     * Upload a text snapshot to RAGuardian.
     *
     * @param string $filename
     * @param string $relative_path
     * @param string $content
     * @return array|WP_Error|null
     */
    public static function upload_snapshot(string $filename, string $relative_path, string $content) {
        $api = new EC_Rag_Api_Client(fn() => EC_Rag_Options::get());

        return $api->post_multipart(
            '/api/v1/files?async=true',
            ['relative_path' => $relative_path],
            [
                'filename'     => $filename,
                'content_type' => 'text/plain; charset=utf-8',
                'content'     => $content,
            ]
        );
    }

    /**
     * Delete a post snapshot from RAGuardian.
     *
     * @param int $post_id
     * @return array|WP_Error|null
     */
    public static function delete_snapshot(int $post_id) {
        $api      = new EC_Rag_Api_Client(fn() => EC_Rag_Options::get());
        $path     = EC_Rag_Utils::sanitize_file_path('wordpress/posts/post-' . $post_id . '.txt');

        return $api->delete('/api/v1/files/' . $path);
    }

    /**
     * WXR import: prepare queue from XML file.
     *
     * @param string $file_path Path to the uploaded WXR file.
     * @return array|WP_Error
     */
    public static function prepare_wxr_queue(string $file_path) {
        if (!class_exists('XMLReader')) {
            return new WP_Error('ec_rag_xmlreader_missing', __('XMLReader is required to parse WordPress export files.', 'ec-rag'));
        }

        if (!is_readable($file_path)) {
            return new WP_Error('ec_rag_wxr_missing', __('WordPress export file is not readable.', 'ec-rag'));
        }

        $upload_dir = wp_upload_dir();
        if (!empty($upload_dir['error'])) {
            return new WP_Error('ec_rag_upload_dir_error', $upload_dir['error']);
        }

        $queue_dir  = trailingslashit($upload_dir['basedir']) . 'raguardian-imports';
        if (!wp_mkdir_p($queue_dir)) {
            return new WP_Error('ec_rag_queue_dir_error', __('Cannot create import queue directory.', 'ec-rag'));
        }

        $queue_path = trailingslashit($queue_dir) . 'ec-rag-wxr-' . wp_generate_uuid4() . '.jsonl';
        $handle     = fopen($queue_path, 'w');
        if (!$handle) {
            return new WP_Error('ec_rag_queue_open_error', __('Cannot write import queue.', 'ec-rag'));
        }

        $reader = new XMLReader();
        if (!$reader->open($file_path, null, LIBXML_NONET | LIBXML_NOCDATA)) {
            fclose($handle);
            wp_delete_file($queue_path);

            return new WP_Error('ec_rag_wxr_parse_error', __('Cannot open WordPress export XML.', 'ec-rag'));
        }

        $total = 0;
        while ($reader->read()) {
            if ($reader->nodeType !== XMLReader::ELEMENT || $reader->name !== 'item') {
                continue;
            }

            $item_xml = $reader->readOuterXML();
            $article  = self::article_from_wxr_item($item_xml);
            if (!$article) {
                continue;
            }

            $encoded = wp_json_encode($article);
            if ($encoded === false || fwrite($handle, $encoded . "\n") === false) {
                $reader->close();
                fclose($handle);
                wp_delete_file($queue_path);

                return new WP_Error('ec_rag_queue_write_error', __('Cannot write import queue.', 'ec-rag'));
            }
            $total++;
        }

        $reader->close();
        fclose($handle);

        $state = [
            'status'       => $total > 0 ? 'queued' : 'empty',
            'queue_path'   => $queue_path,
            'queue_offset' => 0,
            'created_at'   => current_time('mysql'),
            'total'        => $total,
            'processed'    => 0,
            'succeeded'    => 0,
            'failed'       => 0,
            'errors'       => [],
        ];

        $previous_state = self::get_import_state();
        $previous_path  = is_array($previous_state) ? ($previous_state['queue_path'] ?? '') : '';
        if ($previous_path
            && $previous_path !== $queue_path
            && is_file($previous_path)
            && !wp_delete_file($previous_path)) {
            wp_delete_file($queue_path);

            return new WP_Error(
                'ec_rag_previous_queue_delete_error',
                __('Cannot replace the previous import queue. Clear it and retry.', 'ec-rag')
            );
        }

        wp_clear_scheduled_hook(self::CRON_HOOK);
        self::save_import_state($state);

        return $state;
    }

    /**
     * Parse a WXR item XML string into an article array.
     *
     * @param string $item_xml The item XML.
     * @return array|null
     */
    public static function article_from_wxr_item(string $item_xml): ?array {
        $item = simplexml_load_string($item_xml, 'SimpleXMLElement', LIBXML_NOCDATA | LIBXML_NONET);
        if (!$item) {
            return null;
        }

        $namespaces = $item->getNamespaces(true);
        $wp         = isset($namespaces['wp']) ? $item->children($namespaces['wp']) : null;
        $content_ns = isset($namespaces['content']) ? $item->children($namespaces['content']) : null;
        $excerpt_ns = isset($namespaces['excerpt']) ? $item->children($namespaces['excerpt']) : null;

        if (!$wp || (string) $wp->post_type !== 'post' || (string) $wp->status !== 'publish') {
            return null;
        }

        if (trim((string) $wp->post_password) !== '') {
            return null;
        }

        $post_id = absint((string) $wp->post_id);
        if (!$post_id) {
            $post_id = abs(crc32((string) $item->link . '|' . (string) $item->title));
        }

        $content = $content_ns ? (string) $content_ns->encoded : '';
        $excerpt = $excerpt_ns ? (string) $excerpt_ns->encoded : '';

        return [
            'post_id'      => $post_id,
            'title'        => sanitize_text_field((string) $item->title),
            'link'         => esc_url_raw((string) $item->link),
            'published_at' => sanitize_text_field((string) ($wp->post_date ?: $item->pubDate)),
            'excerpt'      => EC_Rag_Utils::html_to_text($excerpt),
            'content'      => EC_Rag_Utils::html_to_text($content),
        ];
    }

    /**
     * Process one batch of the import queue.
     *
     * @return array|WP_Error
     */
    public static function process_import_batch() {
        $state = self::get_import_state();
        if ($state && ($state['status'] ?? '') === 'completed') {
            return true;
        }

        if (!$state || empty($state['queue_path']) || !is_readable($state['queue_path'])) {
            return new WP_Error('ec_rag_no_import_queue', __('No readable import queue.', 'ec-rag'));
        }

        $options    = EC_Rag_Options::get();
        $batch_size = max(1, min(50, absint($options['ingestion_batch_size'] ?? 10)));
        $processed  = absint($state['processed'] ?? 0);
        $total      = absint($state['total'] ?? 0);

        if ($processed >= $total) {
            self::complete_import($state);

            return true;
        }

        $queue = fopen($state['queue_path'], 'r');
        if (!$queue) {
            return new WP_Error('ec_rag_queue_read_error', __('Cannot read import queue.', 'ec-rag'));
        }

        $queue_offset = absint($state['queue_offset'] ?? 0);
        if ($queue_offset > 0) {
            if (fseek($queue, $queue_offset) !== 0) {
                fclose($queue);

                return new WP_Error('ec_rag_queue_seek_error', __('Cannot resume import queue.', 'ec-rag'));
            }
        } elseif ($processed > 0) {
            // Backward compatibility for queues created before byte offsets
            // were persisted: advance line-by-line without loading the file.
            for ($skipped = 0; $skipped < $processed; $skipped++) {
                if (fgets($queue) === false) {
                    fclose($queue);

                    return new WP_Error('ec_rag_queue_seek_error', __('Cannot resume import queue.', 'ec-rag'));
                }
            }
        }

        $state['status'] = 'running';
        $batch_processed = 0;

        while ($batch_processed < $batch_size && $processed < $total) {
            $raw_line = fgets($queue);
            if ($raw_line === false) {
                break;
            }

            $line = trim($raw_line);
            $processed++;
            $batch_processed++;

            $article = $line !== '' ? json_decode($line, true) : null;
            if (!is_array($article)) {
                $state['failed']    = absint($state['failed'] ?? 0) + 1;
                $state['errors'][] = 'Invalid queue item at line ' . $processed;
                $state['processed']  = $processed;
                continue;
            }

            $response = self::ingest_article($article);

            if (is_wp_error($response)) {
                $state['failed']    = absint($state['failed'] ?? 0) + 1;
                $state['errors'][] = 'Post ' . absint($article['post_id'] ?? 0) . ': ' . $response->get_error_message();
            } else {
                $state['succeeded'] = absint($state['succeeded'] ?? 0) + 1;
            }

            $state['processed'] = $processed;
        }

        $next_offset = ftell($queue);
        if ($next_offset !== false) {
            $state['queue_offset'] = $next_offset;
        }
        $ended_early = feof($queue) && $processed < $total;
        fclose($queue);

        if ($ended_early) {
            $missing = $total - $processed;
            $state['failed'] = absint($state['failed'] ?? 0) + $missing;
            $state['errors'][] = sprintf('Import queue ended %d item(s) early.', $missing);
            $processed = $total;
            $state['processed'] = $processed;
        }

        $state['errors'] = array_slice($state['errors'] ?? [], -20);
        if ($processed >= $total) {
            self::complete_import($state);

            return true;
        }

        $state['status'] = 'queued';
        self::save_import_state($state);
        self::schedule_import();

        return true;
    }

    /**
     * Mark an import complete and remove its local queue file.
     *
     * @param array $state Import state.
     * @return void
     */
    protected static function complete_import(array $state): void {
        $queue_path = $state['queue_path'] ?? '';
        if ($queue_path && is_file($queue_path) && !wp_delete_file($queue_path)) {
            $state['errors'][] = __('Import completed, but the local queue file could not be deleted.', 'ec-rag');
        } else {
            $state['queue_path'] = '';
        }

        $state['errors'] = array_slice($state['errors'] ?? [], -20);
        $state['status'] = 'completed';
        wp_clear_scheduled_hook(self::CRON_HOOK);
        self::save_import_state($state);
    }

    /**
     * Schedule the import batch cron.
     *
     * @return void
     */
    public static function schedule_import(): void {
        if (!wp_next_scheduled(self::CRON_HOOK)) {
            wp_schedule_single_event(time() + 5, self::CRON_HOOK);
        }
    }

    /**
     * Cron callback: process import batch.
     *
     * @return void
     */
    public static function cron_process_import_batch(): void {
        self::process_import_batch();
    }

    /**
     * Get import state from options.
     *
     * @return array|null
     */
    public static function get_import_state(): ?array {
        $state = get_option(self::IMPORT_OPTION, []);

        return is_array($state) ? $state : null;
    }

    /**
     * Save import state to options.
     *
     * @param array $state The state to save.
     * @return void
     */
    public static function save_import_state(array $state): void {
        update_option(self::IMPORT_OPTION, $state, false);
    }

    // ---------- Deduplication markers ----------

    /**
     * Mark a post as handled in the current request.
     *
     * @param int $post_id
     * @return void
     */
    public static function mark_handled(int $post_id): void {
        $GLOBALS['ec_rag_sync_handled_posts'][absint($post_id)] = true;
    }

    /**
     * Check if a post was already handled.
     *
     * @param int $post_id
     * @return bool
     */
    public static function was_handled(int $post_id): bool {
        $post_id = absint($post_id);

        return !empty($GLOBALS['ec_rag_sync_handled_posts'][$post_id]);
    }
}
