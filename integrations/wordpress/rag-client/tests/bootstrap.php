<?php
/**
 * PHPUnit bootstrap - WordPress function stubs + autoload.
 */

define('ABSPATH', dirname(__DIR__));
define('EC_RAG_TESTING', true);
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
if (!function_exists('wp_strip_all_tags')) {
    function wp_strip_all_tags($value, $_ = false) { return strip_tags($value); }
}
if (!function_exists('esc_url_raw')) {
    function esc_url_raw($url) { return htmlspecialchars_decode($url, ENT_QUOTES); }
}
if (!function_exists('wp_unslash')) {
    function wp_unslash($value) { return stripslashes($value); }
}
if (!function_exists('wp_parse_args')) {
    function wp_parse_args($args, $defaults) {
        return is_array($args) ? array_merge($defaults, $args) : $defaults;
    }
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
    function get_option($key, $default = false) { return $default; }
}
if (!function_exists('update_option')) {
    function update_option($key, $value, $autoload = null) { return true; }
}
if (!function_exists('delete_option')) {
    function delete_option($key) { return true; }
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
