<?php
/**
 * Agent policy, proxy validation, and public-catalog tests.
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Agents_Tests extends TestCase {

    protected function setUp(): void {
        $GLOBALS['ec_rag_test_transients'] = [];
        $GLOBALS['ec_rag_test_options'] = [];
    }

    public function test_admin_payload_requires_exactly_one_prompt_reference(): void {
        $payload = \EC_Rag_Ajax_Agents::sanitize_payload([
            'name' => 'Support',
            'provider_id' => 'openai',
            'model_id' => 'gpt-4o',
            'knowledge_base_ids' => ['default'],
            'prompt_ref' => [],
        ], true);

        self::assertInstanceOf(\WP_Error::class, $payload);
        self::assertSame('ec_rag_invalid_agent_payload', $payload->get_error_code());
    }

    public function test_admin_payload_preserves_kb_order_and_prompt_scope(): void {
        $kb_id = 'kb_' . str_repeat('1', 32);
        $payload = \EC_Rag_Ajax_Agents::sanitize_payload([
            'name' => 'Support',
            'description' => 'Answers questions',
            'provider_id' => 'openai',
            'model_id' => 'gpt-4o',
            'knowledge_base_ids' => [$kb_id, 'default', $kb_id],
            'prompt_ref' => ['id' => 'prompt-1', 'scope' => 'shared'],
            'unexpected' => 'discarded',
        ], true);

        self::assertSame([$kb_id, 'default'], $payload['knowledge_base_ids']);
        self::assertSame(['id' => 'prompt-1', 'scope' => 'shared'], $payload['prompt_ref']);
        self::assertArrayNotHasKey('unexpected', $payload);
    }

    public function test_public_selection_requires_allowlist_and_live_availability(): void {
        $allowed_id = 'agent_' . str_repeat('a', 32);
        $other_id = 'agent_' . str_repeat('b', 32);
        $options = ['allowed_agent_ids' => [$allowed_id]];
        $agents = [
            ['id' => $allowed_id, 'available' => true],
            ['id' => $other_id, 'available' => true],
        ];

        self::assertSame(
            $allowed_id,
            \EC_Rag_Ajax::validate_public_agent_selection($allowed_id, $options, $agents)
        );
        self::assertInstanceOf(
            \WP_Error::class,
            \EC_Rag_Ajax::validate_public_agent_selection($other_id, $options, $agents)
        );
        $agents[0]['available'] = false;
        self::assertInstanceOf(
            \WP_Error::class,
            \EC_Rag_Ajax::validate_public_agent_selection($allowed_id, $options, $agents)
        );
    }

    public function test_public_catalog_exposes_only_minimal_allowlisted_records(): void {
        $allowed_id = 'agent_' . str_repeat('c', 32);
        $hidden_id = 'agent_' . str_repeat('d', 32);
        $GLOBALS['ec_rag_test_http_handler'] = function () use ($allowed_id, $hidden_id) {
            return [
                'response' => ['code' => 200],
                'body' => json_encode([
                    'agents' => [
                        [
                            'id' => $allowed_id,
                            'name' => 'Public assistant',
                            'description' => 'Visible description',
                            'available' => true,
                            'provider_id' => 'secret-provider',
                            'knowledge_base_ids' => ['default'],
                        ],
                        [
                            'id' => $hidden_id,
                            'name' => 'Hidden assistant',
                            'available' => true,
                        ],
                    ],
                    'capabilities' => ['can_manage' => false],
                ]),
                'headers' => [],
            ];
        };
        $options = array_merge(\EC_Rag_Options::defaults(), [
            'base_url' => 'https://rag.example.test',
            'api_key' => 'query-key',
            'allowed_agent_ids' => [$allowed_id],
        ]);

        $catalog = \EC_Rag_Widget::public_agent_catalog($options);

        self::assertSame([[
            'id' => $allowed_id,
            'name' => 'Public assistant',
            'description' => 'Visible description',
        ]], $catalog);
        self::assertArrayNotHasKey('provider_id', $catalog[0]);
        self::assertArrayNotHasKey('knowledge_base_ids', $catalog[0]);
    }
}
