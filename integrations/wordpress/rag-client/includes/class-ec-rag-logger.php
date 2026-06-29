<?php
/**
 * EC_Rag_Logger
 *
 * Persistent error logger for RAGuardian API calls.
 * Stores errors in a site option with retention limit.
 * Does not log API keys or sensitive payload data.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Logger {

    /** Max number of errors to retain. */
    const MAX_ENTRIES = 100;

    /** The option key for stored errors. */
    const ERROR_LOG_OPTION = 'ec_rag_error_log';

    /**
     * Log an error message.
     *
     * @param string $message  The error message.
     * @param string $context  The source context (e.g. 'health_check', 'api_query').
     * @param int    $severity 1=critical, 2=error, 3=warning.
     * @return void
     */
    public static function log(string $message, string $context = 'unknown', int $severity = 2): void {
        $entry = [
            'time'     => current_time('mysql'),
            'message'  => $message,
            'context'  => $context,
            'severity' => $severity,
        ];

        $all   = get_option(self::ERROR_LOG_OPTION, []);
        $all[] = $entry;

        // Trim to MAX_ENTRIES.
        if (count($all) > self::MAX_ENTRIES) {
            $all = array_slice($all, -self::MAX_ENTRIES);
        }

        update_option(self::ERROR_LOG_OPTION, $all);
    }

    /**
     * Retrieve recent log entries.
     *
     * @param int $limit Number of entries to return.
     * @return array<int, array<string, mixed>>
     */
    public static function get_recent(int $limit = 20): array {
        $all = get_option(self::ERROR_LOG_OPTION, []);

        return array_slice($all, -$limit);
    }

    /**
     * Clear the error log.
     *
     * @return void
     */
    public static function clear(): void {
        delete_option(self::ERROR_LOG_OPTION);
    }
}
