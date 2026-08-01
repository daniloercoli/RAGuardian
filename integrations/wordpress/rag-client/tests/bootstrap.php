<?php
/**
 * PHPUnit bootstrap - WordPress function stubs + autoload.
 */

define('ABSPATH', dirname(__DIR__));
define('EC_RAG_TESTING', true);
if (!defined('MINUTE_IN_SECONDS')) {
    define('MINUTE_IN_SECONDS', 60);
}
if (!defined('HOUR_IN_SECONDS')) {
    define('HOUR_IN_SECONDS', 3600);
}
require_once dirname(__DIR__) . '/vendor/autoload.php';

// Stub plugin_dir_path so the autoloader works.
if (!function_exists('plugin_dir_path')) {
    function plugin_dir_path($file) {
        $path = realpath(dirname($file));
        $url = untrailingslashit($path);
        return trailingslashit($url);
    }
    function untrailingslashit($text) { return rtrim($text, '/\\'); }
    function trailingslashit($text) { return $text . '/'; }
}

require_once dirname(__DIR__) . '/includes/autoload.php';

// Global WordPress stubs for unit tests.
if (!function_exists('sanitize_text_field')) {
    function sanitize_text_field($value) { return trim(stripslashes($value)); }
}
if (!function_exists('sanitize_key')) {
    function sanitize_key($value) { return trim(strtolower($value)); }
}
if (!function_exists('sanitize_hex_color')) {
    function sanitize_hex_color($value) {
        if (empty($value)) { return ''; }
        $color = ltrim($value, '#');
        return preg_match('/^[a-f0-9]{6}$/i', $color) ? '#' . $color : '';
    }
}
if (!function_exists('sanitize_textarea_field')) {
    function sanitize_textarea_field($value) { return trim(strip_tags($value)); }
}
if (!function_exists('sanitize_file_name')) {
    function sanitize_file_name($value) {
        return preg_replace('/[^A-Za-z0-9._-]/', '-', basename((string) $value));
    }
}
if (!function_exists('wp_strip_all_tags')) {
    function wp_strip_all_tags($value, $_ = false) { return strip_tags($value); }
}
if (!function_exists('esc_url_raw')) {
    function esc_url_raw($url) { return htmlspecialchars_decode($url, ENT_QUOTES); }
}
if (!function_exists('esc_attr')) {
    function esc_attr($value) { return htmlspecialchars((string) $value, ENT_QUOTES); }
}
if (!function_exists('esc_html')) {
    function esc_html($value) { return htmlspecialchars((string) $value); }
}
if (!function_exists('esc_url')) {
    function esc_url($value) { return htmlspecialchars((string) $value, ENT_QUOTES); }
}
if (!function_exists('selected')) {
    function selected($selected, $current, $display = true) {
        $result = ((string) $selected === (string) $current)
            ? 'selected="selected"'
            : '';
        if ($display) {
            echo $result;
        }
        return $result;
    }
}
if (!function_exists('admin_url')) {
    function admin_url($path = '') { return 'https://example.test/wp-admin/' . ltrim($path, '/'); }
}
if (!function_exists('wp_nonce_url')) {
    function wp_nonce_url($url, $action = -1, $name = '_wpnonce') {
        return add_query_arg($name, (string) $action, $url);
    }
}
if (!function_exists('wp_unslash')) {
    function wp_unslash($value) { return stripslashes($value); }
}
if (!function_exists('wp_parse_args')) {
    function wp_parse_args($args, $defaults) {
        return is_array($args) ? array_merge($defaults, $args) : $defaults;
    }
}
if (!function_exists('add_query_arg')) {
    function add_query_arg($key, $value = null, $url = null) {
        if (is_array($key)) {
            $url = (string) $value;
            foreach ($key as $name => $item) {
                $url = add_query_arg($name, $item, $url);
            }
            return $url;
        }
        $url = (string) $url;
        $separator = str_contains($url, '?') ? '&' : '?';
        return $url . $separator . rawurlencode((string) $key) . '=' . rawurlencode((string) $value);
    }
}
if (!function_exists('checked')) {
    function checked($checked, $current = true, $display = true) {
        $result = ((string) $checked === (string) $current) ? 'checked="checked"' : '';
        if ($display) { echo $result; }
        return $result;
    }
}
if (!function_exists('disabled')) {
    function disabled($disabled, $current = true, $display = true) {
        $result = ((string) $disabled === (string) $current) ? 'disabled="disabled"' : '';
        if ($display) { echo $result; }
        return $result;
    }
}
if (!function_exists('wp_create_nonce')) {
    function wp_create_nonce($action = -1) { return 'nonce-' . $action; }
}
if (!function_exists('wp_kses_post')) {
    function wp_kses_post($value) { return $value; }
}
if (!function_exists('strip_shortcodes')) {
    function strip_shortcodes($value) { return $value; }
}
if (!function_exists('absint')) {
    function absint($value) { return abs((int) $value); }
}
if (!function_exists('get_option')) {
    function get_option($key, $default = false) {
        return array_key_exists($key, $GLOBALS['ec_rag_test_options'] ?? [])
            ? $GLOBALS['ec_rag_test_options'][$key]
            : $default;
    }
}
if (!function_exists('update_option')) {
    function update_option($key, $value, $autoload = null) {
        $GLOBALS['ec_rag_test_options'][$key] = $value;
        return true;
    }
}
if (!function_exists('delete_option')) {
    function delete_option($key) {
        unset($GLOBALS['ec_rag_test_options'][$key]);
        return true;
    }
}
if (!function_exists('is_user_logged_in')) {
    function is_user_logged_in() {
        return !empty($GLOBALS['ec_rag_test_user_id']);
    }
}
if (!function_exists('get_current_user_id')) {
    function get_current_user_id() {
        return (int) ($GLOBALS['ec_rag_test_user_id'] ?? 0);
    }
}
if (!function_exists('set_transient')) {
    function set_transient($key, $value, $ttl) {
        $GLOBALS['ec_rag_test_transients'][$key] = [
            'value'   => $value,
            'expires' => time() + (int) $ttl,
        ];
        return true;
    }
}
if (!function_exists('get_transient')) {
    function get_transient($key) {
        if (empty($GLOBALS['ec_rag_test_transients'][$key])) {
            return false;
        }
        if ($GLOBALS['ec_rag_test_transients'][$key]['expires'] <= time()) {
            unset($GLOBALS['ec_rag_test_transients'][$key]);
            return false;
        }
        return $GLOBALS['ec_rag_test_transients'][$key]['value'];
    }
}
if (!function_exists('delete_transient')) {
    function delete_transient($key) {
        unset($GLOBALS['ec_rag_test_transients'][$key]);
        return true;
    }
}
if (!function_exists('wp_next_scheduled')) {
    function wp_next_scheduled($hook, $args = []) {
        foreach ($GLOBALS['ec_rag_test_scheduled_events'] ?? [] as $event) {
            if ($event['hook'] === $hook && $event['args'] === $args) {
                return $event['timestamp'];
            }
        }
        return false;
    }
}
if (!function_exists('wp_schedule_single_event')) {
    function wp_schedule_single_event($timestamp, $hook, $args = []) {
        $GLOBALS['ec_rag_test_scheduled_events'][] = [
            'timestamp' => (int) $timestamp,
            'hook'      => $hook,
            'args'      => $args,
        ];
        return true;
    }
}
if (!function_exists('wp_clear_scheduled_hook')) {
    function wp_clear_scheduled_hook($hook, $args = []) {
        $GLOBALS['ec_rag_test_scheduled_events'] = array_values(
            array_filter(
                $GLOBALS['ec_rag_test_scheduled_events'] ?? [],
                fn ($event) => $event['hook'] !== $hook || $event['args'] !== $args
            )
        );
        return 1;
    }
}
if (!function_exists('wp_unschedule_hook')) {
    function wp_unschedule_hook($hook) {
        $GLOBALS['ec_rag_test_scheduled_events'] = array_values(
            array_filter(
                $GLOBALS['ec_rag_test_scheduled_events'] ?? [],
                fn ($event) => $event['hook'] !== $hook
            )
        );
        return 1;
    }
}
if (!function_exists('wp_delete_file')) {
    function wp_delete_file($path) {
        return is_file($path) ? unlink($path) : false;
    }
}
if (!function_exists('wp_json_encode')) {
    function wp_json_encode($value) { return json_encode($value); }
}
if (!function_exists('wp_generate_uuid4')) {
    function wp_generate_uuid4() { return '00000000-0000-4000-8000-000000000000'; }
}
if (!function_exists('apply_filters')) {
    function apply_filters($hook, $value, ...$args) {
        if (!empty($GLOBALS['ec_rag_test_filters'][$hook]) && is_callable($GLOBALS['ec_rag_test_filters'][$hook])) {
            return call_user_func($GLOBALS['ec_rag_test_filters'][$hook], $value, ...$args);
        }
        return $value;
    }
}
if (!class_exists('WP_Error')) {
    class WP_Error {
        private $code;
        private $message;

        public function __construct($code = '', $message = '') {
            $this->code = $code;
            $this->message = $message;
        }

        public function get_error_code() {
            return $this->code;
        }

        public function get_error_message() {
            return $this->message;
        }
    }
}
if (!function_exists('is_wp_error')) {
    function is_wp_error($value) { return $value instanceof WP_Error; }
}
if (!function_exists('wp_remote_get')) {
    function wp_remote_get($url, $args = []) {
        return call_user_func($GLOBALS['ec_rag_test_http_handler'], 'GET', $url, $args);
    }
}
if (!function_exists('wp_remote_post')) {
    function wp_remote_post($url, $args = []) {
        return call_user_func($GLOBALS['ec_rag_test_http_handler'], 'POST', $url, $args);
    }
}
if (!function_exists('wp_remote_request')) {
    function wp_remote_request($url, $args = []) {
        return call_user_func($GLOBALS['ec_rag_test_http_handler'], $args['method'] ?? 'GET', $url, $args);
    }
}
if (!function_exists('wp_remote_retrieve_response_code')) {
    function wp_remote_retrieve_response_code($response) {
        return (int) ($response['response']['code'] ?? 0);
    }
}
if (!function_exists('wp_remote_retrieve_body')) {
    function wp_remote_retrieve_body($response) {
        return (string) ($response['body'] ?? '');
    }
}
if (!function_exists('wp_remote_retrieve_header')) {
    function wp_remote_retrieve_header($response, $header) {
        $headers = $response['headers'] ?? [];
        $header = strtolower($header);
        foreach ($headers as $name => $value) {
            if (strtolower($name) === $header) {
                return $value;
            }
        }
        return '';
    }
}
if (!isset($GLOBALS['ec_rag_test_http_handler'])) {
    $GLOBALS['ec_rag_test_http_handler'] = function () {
        return [
            'response' => ['code' => 200],
            'body'     => '{}',
            'headers'  => [],
        ];
    };
}

// Translation stub.
if (!function_exists('__')) {
    function __($text, $domain = 'default') { return $text; }
}
if (!function_exists('esc_html__')) {
    function esc_html__($text, $domain = 'default') { return htmlspecialchars($text); }
}
if (!function_exists('esc_html_e')) {
    function esc_html_e($text, $domain = 'default') { return htmlspecialchars($text); }
}
