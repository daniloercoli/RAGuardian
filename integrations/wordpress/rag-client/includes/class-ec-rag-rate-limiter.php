<?php
/**
 * EC_Rag_Rate_Limiter
 *
 * WordPress transient-based rate limiting with sliding window.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Rate_Limiter {

    /**
     * Check rate limit for an action.
     *
     * @param string $action Action identifier (query|tts|audio).
     * @param int    $max_requests Max requests per window.
     * @param int    $window_seconds Window duration.
     * @return bool|WP_Error True if allowed, WP_Error if rate limited.
     */
    public function check(string $action, int $max_requests, int $window_seconds) {
        $max_requests = max(1, $max_requests);
        $window_secs  = max(10, $window_seconds);

        // Never include the client-controlled conversation ID in the bucket key:
        // callers could otherwise rotate it to bypass every configured limit.
        $identity = is_user_logged_in()
            ? 'user:' . absint(get_current_user_id())
            : 'ip:' . EC_Rag_Utils::client_ip();
        $identity = (string) apply_filters('ec_rag_rate_limit_identity', $identity, $action);
        $key      = 'ec_rag_rl_' . md5($action . '|' . $identity);

        $bucket = get_transient($key);
        $now    = time();

        // New bucket or expired.
        if (!is_array($bucket) || empty($bucket['reset']) || absint($bucket['reset']) <= $now) {
            set_transient($key, ['count' => 1, 'reset' => $now + $window_secs], $window_secs);

            return true;
        }

        $count = absint($bucket['count'] ?? 0);

        if ($count >= $max_requests) {
            $retry = max(1, absint($bucket['reset']) - $now);

            return new WP_Error(
                'ec_rag_rate_limited',
                sprintf(__('Rate limit exceeded. Try again in %d seconds.', 'ec-rag'), $retry)
            );
        }

        // Increment counter.
        $bucket['count'] = $count + 1;
        set_transient($key, $bucket, max(1, absint($bucket['reset']) - $now));

        return true;
    }
}
