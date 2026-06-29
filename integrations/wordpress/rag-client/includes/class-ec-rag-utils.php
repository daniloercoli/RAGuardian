<?php
/**
 * EC_Rag_Utils
 *
 * Static helper functions used across the plugin.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Utils {

    /**
     * Check if a value is truthy.
     *
     * @param mixed $value The value to check.
     * @return bool
     */
    public static function is_truthy($value): bool {
        return in_array(strtolower((string) $value), ['1', 'true', 'yes', 'on'], true);
    }

    /**
     * Sanitize and validate response language.
     *
     * @param string $value The language code.
     * @return string
     */
    public static function sanitize_response_language(string $value): string {
        $language = strtolower(sanitize_key($value));

        return in_array($language, ['auto', 'it', 'en'], true) ? $language : 'auto';
    }

    /**
     * Convert HTML to clean text.
     *
     * @param string $html The HTML input.
     * @return string
     */
    public static function html_to_text(string $html): string {
        if ($html === '') {
            return '';
        }

        $html = (string) strip_shortcodes($html);
        $html = wp_kses_post($html);
        $html = preg_replace('/<\s*br\s*\/*\s*>/i', "\n", $html);
        $html = preg_replace('/<\/\s*p\s*>/i', "\n\n", $html);

        $text = wp_strip_all_tags($html, true);
        $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $text = preg_replace("/[ \t]+/", ' ', $text);
        $text = preg_replace("/\n{3,}/", "\n\n", $text);

        return trim($text);
    }

    /**
     * Get the client IP address.
     *
     * @return string
     */
    public static function client_ip(): string {
        $ip = sanitize_text_field(wp_unslash($_SERVER['REMOTE_ADDR'] ?? ''));

        return $ip ?: '127.0.0.1';
    }

    /**
     * Build a unique conversation ID from request.
     *
     * @return string
     */
    public static function conversation_id_from_request(): string {
        if (!empty($_POST['conversation_id'])) {
            return sanitize_text_field(wp_unslash($_POST['conversation_id']));
        }

        if (!empty($_COOKIE['ec_rag_conversation_id'])) {
            return sanitize_text_field(wp_unslash($_COOKIE['ec_rag_conversation_id']));
        }

        return '';
    }

    /**
     * Build the current page context for the API.
     *
     * @return array<string, mixed>
     */
    public static function page_context(): array {
        $post = get_queried_object();

        return [
            'page_title'  => $post ? get_the_title($post) : wp_get_document_title(),
            'page_url'    => home_url(add_query_arg([], $_SERVER['REQUEST_URI'] ?? '')),
            'post_type'   => $post ? $post->post_type : '',
            'locale'      => determine_locale(),
        ];
    }

    /**
     * Build client context from request + global options.
     *
     * @param array $options Plugin options.
     * @return array
     */
    public static function client_context(array $options): array {
        $instructions = [];

        if (!empty($options['global_context'])) {
            $instructions[] = $options['global_context'];
        }

        $shortcode_context = sanitize_textarea_field(wp_unslash($_POST['context'] ?? ''));
        if ($shortcode_context !== '') {
            $instructions[] = $shortcode_context;
        }

        $context = [
            'site_name'    => get_bloginfo('name'),
            'page_title'   => sanitize_text_field(wp_unslash($_POST['page_title'] ?? '')),
            'page_url'     => esc_url_raw(wp_unslash($_POST['page_url'] ?? '')),
            'post_type'    => sanitize_key(wp_unslash($_POST['post_type'] ?? '')),
            'locale'      => determine_locale(),
            'instructions' => implode("\n", $instructions),
        ];

        return array_filter(
            $context,
            fn ($value) => $value !== ''
        );
    }

    /**
     * Sanitize a relative file path for the API.
     *
     * @param string $path The raw path.
     * @return string The URL-encoded safe path.
     */
    public static function sanitize_file_path(string $path): string {
        $parts = array_map(
            fn ($part) => rawurlencode($part),
            array_filter(explode('/', str_replace('\\', '/', $path)))
        );

        return $parts ? implode('/', $parts) : $path;
    }

    /**
     * Check if a post is a public article.
     *
     * @param WP_Post|null $post The post object.
     * @return bool
     */
    public static function is_public_article($post): bool {
        if (!$post || $post->post_type !== 'post') {
            return false;
        }

        return $post->post_status === 'publish'
            && trim((string) $post->post_password) === '';
    }

    /**
     * Build an article snapshot from a post object.
     *
     * @param WP_Post $post The post.
     * @return array
     */
    public static function article_from_post($post): array {
        $content = apply_filters('the_content', $post->post_content);
        $excerpt = has_excerpt($post) ? get_the_excerpt($post) : '';

        return [
            'post_id'      => absint($post->ID),
            'title'        => sanitize_text_field(get_the_title($post)),
            'link'         => esc_url_raw(get_permalink($post)),
            'published_at' => sanitize_text_field(get_post_time('c', true, $post)),
            'excerpt'      => self::html_to_text($excerpt),
            'content'      => self::html_to_text($content),
        ];
    }

    /**
     * Build the content text for an article snapshot.
     *
     * @param array $article The article data.
     * @return string
     */
    public static function snapshot_content(array $article): string {
        $parts = [
            'Title: ' . sanitize_text_field($article['title'] ?? ''),
            'URL: ' . esc_url_raw($article['link'] ?? ''),
            'Published: ' . sanitize_text_field($article['published_at'] ?? ''),
        ];

        $excerpt = trim((string) ($article['excerpt'] ?? ''));
        if ($excerpt !== '') {
            $parts[] = 'Excerpt: ' . $excerpt;
        }

        $parts[] = '';
        $parts[] = trim((string) ($article['content'] ?? ''));

        return trim(implode("\n", $parts)) . "\n";
    }
}
