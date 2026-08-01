<?php
/**
 * Authenticated wp-admin proxy for RAGuardian Agent CRUD.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

class EC_Rag_Ajax_Agents {

    const NONCE_ACTION = 'ec_rag_agents_admin';

    /** Register authenticated actions only. */
    public static function register(): void {
        add_action('wp_ajax_ec_rag_list_agents', [self::class, 'list_agents']);
        add_action('wp_ajax_ec_rag_agent_options', [self::class, 'agent_options']);
        add_action('wp_ajax_ec_rag_create_agent', [self::class, 'create_agent']);
        add_action('wp_ajax_ec_rag_update_agent', [self::class, 'update_agent']);
        add_action('wp_ajax_ec_rag_delete_agent', [self::class, 'delete_agent']);
    }

    public static function list_agents(): void {
        self::guard();
        self::send((new EC_Rag_Api_Client([EC_Rag_Options::class, 'get']))->get_agent_catalog());
    }

    public static function agent_options(): void {
        self::guard();
        self::send((new EC_Rag_Api_Client([EC_Rag_Options::class, 'get']))->get_agent_options());
    }

    public static function create_agent(): void {
        self::guard();
        $payload = self::sanitize_payload(self::posted_payload(), true);
        if (is_wp_error($payload)) {
            self::send($payload, 400);
        }
        $api = new EC_Rag_Api_Client([EC_Rag_Options::class, 'get']);
        self::send($api->create_agent(
            $payload['name'],
            $payload['description'],
            $payload['provider_id'],
            $payload['model_id'],
            $payload['knowledge_base_ids'],
            $payload['prompt_ref']
        ));
    }

    public static function update_agent(): void {
        self::guard();
        $agent_id = EC_Rag_Options::sanitize_agent_id(
            wp_unslash($_POST['agent_id'] ?? '')
        );
        if ($agent_id === '') {
            self::send(new WP_Error('ec_rag_invalid_agent', __('Invalid Agent ID', 'ec-rag')), 400);
        }
        $payload = self::sanitize_payload(self::posted_payload(), false);
        if (is_wp_error($payload)) {
            self::send($payload, 400);
        }
        self::send(
            (new EC_Rag_Api_Client([EC_Rag_Options::class, 'get']))
                ->update_agent($agent_id, $payload)
        );
    }

    public static function delete_agent(): void {
        self::guard();
        $agent_id = EC_Rag_Options::sanitize_agent_id(
            wp_unslash($_POST['agent_id'] ?? '')
        );
        if ($agent_id === '') {
            self::send(new WP_Error('ec_rag_invalid_agent', __('Invalid Agent ID', 'ec-rag')), 400);
        }
        self::send(
            (new EC_Rag_Api_Client([EC_Rag_Options::class, 'get']))
                ->delete_agent($agent_id)
        );
    }

    /** Validate and normalize a create/update payload. */
    public static function sanitize_payload(array $raw, bool $require_all) {
        $allowed = [
            'name', 'description', 'provider_id', 'model_id',
            'knowledge_base_ids', 'prompt_ref',
        ];
        $payload = [];
        foreach ($allowed as $field) {
            if (array_key_exists($field, $raw)) {
                $payload[$field] = $raw[$field];
            }
        }
        if ($require_all) {
            foreach (['name', 'provider_id', 'model_id', 'knowledge_base_ids', 'prompt_ref'] as $field) {
                if (!array_key_exists($field, $payload)) {
                    return new WP_Error('ec_rag_invalid_agent_payload', __('Missing Agent fields', 'ec-rag'));
                }
            }
        }
        foreach (['name', 'description', 'provider_id', 'model_id'] as $field) {
            if (array_key_exists($field, $payload)) {
                $payload[$field] = sanitize_text_field((string) $payload[$field]);
            }
        }
        if (isset($payload['name']) && ($payload['name'] === '' || strlen($payload['name']) > 120)) {
            return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid Agent name', 'ec-rag'));
        }
        if (isset($payload['description']) && strlen($payload['description']) > 500) {
            return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid Agent description', 'ec-rag'));
        }
        if (isset($payload['provider_id']) && $payload['provider_id'] === '') {
            return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid provider', 'ec-rag'));
        }
        if (isset($payload['model_id']) && $payload['model_id'] === '') {
            return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid model', 'ec-rag'));
        }
        if (array_key_exists('knowledge_base_ids', $payload)) {
            if (!is_array($payload['knowledge_base_ids'])) {
                return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid knowledge bases', 'ec-rag'));
            }
            $ids = [];
            foreach ($payload['knowledge_base_ids'] as $value) {
                $value = sanitize_text_field((string) $value);
                if ($value !== 'default' && !preg_match('/^kb_[0-9a-f]{32}$/', $value)) {
                    return new WP_Error('ec_rag_invalid_agent_payload', __('Invalid knowledge base', 'ec-rag'));
                }
                if (!in_array($value, $ids, true)) {
                    $ids[] = $value;
                }
            }
            if (!$ids) {
                return new WP_Error('ec_rag_invalid_agent_payload', __('Select a knowledge base', 'ec-rag'));
            }
            $payload['knowledge_base_ids'] = $ids;
        }
        if (array_key_exists('prompt_ref', $payload)) {
            $prompt_ref = is_array($payload['prompt_ref']) ? $payload['prompt_ref'] : [];
            $prompt_id = sanitize_text_field((string) ($prompt_ref['id'] ?? ''));
            $scope = sanitize_key($prompt_ref['scope'] ?? '');
            if ($prompt_id === '' || !in_array($scope, ['personal', 'shared'], true)) {
                return new WP_Error('ec_rag_invalid_agent_payload', __('Select a system prompt', 'ec-rag'));
            }
            $payload['prompt_ref'] = ['id' => $prompt_id, 'scope' => $scope];
        }
        if (!array_key_exists('description', $payload) && $require_all) {
            $payload['description'] = '';
        }
        if (!$require_all && !$payload) {
            return new WP_Error('ec_rag_invalid_agent_payload', __('No Agent fields supplied', 'ec-rag'));
        }
        return $payload;
    }

    protected static function posted_payload(): array {
        $decoded = json_decode(wp_unslash($_POST['payload'] ?? ''), true);
        return is_array($decoded) ? $decoded : [];
    }

    protected static function guard(): void {
        check_ajax_referer(self::NONCE_ACTION, 'nonce');
        if (!current_user_can('manage_options')) {
            wp_send_json_error(['message' => __('Permission denied', 'ec-rag')], 403);
        }
    }

    protected static function send($result, int $error_status = 502): void {
        if (is_wp_error($result)) {
            wp_send_json_error(['message' => $result->get_error_message()], $error_status);
        }
        wp_send_json_success($result);
    }
}
