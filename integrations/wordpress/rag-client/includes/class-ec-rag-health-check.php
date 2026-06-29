<?php
/**
 * EC_Rag_Health_Check
 *
 * Periodic health checks for RAGuardian connectivity.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Health_Check {

    /** The status check interval (in minutes). */
    const CHECK_INTERVAL = 60;

    /** The transient key for status. */
    const STATUS_TRANSIENT = 'ec_rag_health_status';

    /** Number of consecutive failures before alerting. */
    const FAILURE_THRESHOLD = 3;

    /**
     * Register the health check cron event.
     *
     * @return void
     */
    public static function register(): void {
        add_filter('cron_schedules', [self::class, 'cron_schedules']);
        add_action('init', [self::class, 'schedule_cron']);
        add_action('ec_rag_health_check', [self::class, 'run']);
        add_action('admin_bar_menu', [self::class, 'admin_bar'], 99);
        add_action('admin_notices', [self::class, 'admin_notice']);
    }

    /**
     * Register a stable hourly schedule for health checks.
     *
     * @param array $schedules Existing WordPress cron schedules.
     * @return array
     */
    public static function cron_schedules(array $schedules): array {
        $schedules['ec_rag_hourly'] = [
            'interval' => MINUTE_IN_SECONDS * self::CHECK_INTERVAL,
            'display'  => __('Every hour for RAGuardian health checks', 'ec-rag'),
        ];

        return $schedules;
    }

    /**
     * Schedule the cron event if not already scheduled.
     *
     * @return void
     *
     * @wp-hook init
     */
    public static function schedule_cron(): void {
        if (!wp_next_scheduled('ec_rag_health_check')) {
            wp_schedule_event(
                time() + MINUTE_IN_SECONDS * self::CHECK_INTERVAL,
                'ec_rag_hourly',
                'ec_rag_health_check'
            );
        }
    }

    /**
     * Clear scheduled jobs on plugin deactivation.
     *
     * @return void
     */
    public static function deactivate(): void {
        wp_clear_scheduled_hook('ec_rag_health_check');
    }

    /**
     * Run the health check.
     *
     * @return void
     *
     * @wp-hook ec_rag_health_check
     */
    public static function run(): void {
        $options = EC_Rag_Options::get();

        // Skip if not configured.
        if (empty($options['base_url']) || empty($options['api_key'])) {
            return;
        }

        $api = new EC_Rag_Api_Client(fn() => $options);
        $response = $api->get('/api/v1/health');

        $current = get_option('ec_rag_consecutive_failures', 0);

        if (!is_wp_error($response)) {
            // Success.
            update_option('ec_rag_health_status', 'healthy');
            update_option('ec_rag_consecutive_failures', 0);

            return;
        }

        // Failure.
        $current++;
        update_option('ec_rag_health_status', 'unhealthy');
        update_option('ec_rag_consecutive_failures', $current);

        EC_Rag_Logger::log($response->get_error_message(), 'health_check', 3);
    }

    /**
     * Add admin bar menu item.
     *
     * @param WP_Admin_Bar $bar
     * @return void
     *
     * @wp-hook admin_bar_menu
     */
    public static function admin_bar($bar): void {
        if (!current_user_can('manage_options')) {
            return;
        }

        $status = get_option('ec_rag_health_status', 'unknown');
        $failures = get_option('ec_rag_consecutive_failures', 0);

        $titles = [
            'healthy'   => '✅ RAGuardian connected',
            'unhealthy' => '⚠️ RAGuardian disconnected',
            'unknown'   => '🔍 RAGuardian checking...',
        ];

        $icon = '✓';
        if ($status === 'unhealthy') {
            $icon = '✗';
        } elseif ($status === 'unknown') {
            $icon = '?';
        }

        $bar->add_node([
            'id'    => 'ec-rag-health',
            'title' => $icon . ' RAGuardian',
            'href'  => admin_url('options-general.php?page=ec-rag-client'),
            'meta'  => [
                'title' => sprintf(
                    '%s (status: %s%s)',
                    $titles[$status] ?? $status,
                    $status,
                    $failures > 0 ? sprintf(' - %d failures', $failures) : ''
                ),
            ],
        ]);
    }

    /**
     * Display admin notice on health issues.
     *
     * @return void
     *
     * @wp-hook admin_notices
     */
    public static function admin_notice(): void {
        $status = get_option('ec_rag_health_status', 'unknown');

        if ($status !== 'unhealthy') {
            return;
        }

        $failures = get_option('ec_rag_consecutive_failures', 0);

        if ($failures < self::FAILURE_THRESHOLD) {
            return;
        }

        ?>
        <div class="notice notice-error is-dismissible">
            <p>
                <strong><?php esc_html_e('RAGuardian Connection Issue', 'ec-rag'); ?></strong>
                <?php
                printf(
                    esc_html__('Cannot reach RAGuardian server after %d consecutive failures. Please check the connection settings.', 'ec-rag'),
                    $failures
                );
                ?>
                <a href="<?php echo esc_url(admin_url('options-general.php?page=ec-rag-client')); ?>">
                    <?php esc_html_e('Open settings', 'ec-rag'); ?>
                </a>
            </p>
        </div>
        <?php
    }

    /**
     * Get the current health status.
     *
     * @return string
     */
    public static function get_status(): string {
        return get_option(self::STATUS_TRANSIENT, 'unknown');
    }
}
