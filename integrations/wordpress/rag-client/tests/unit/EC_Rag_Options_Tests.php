<?php
/**
 * EC_Rag_Options_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Options_Tests extends TestCase {

    public function test_defaults(): void {
        $defaults = \EC_Rag_Options::defaults();
        self::assertArrayHasKey('base_url', $defaults);
        self::assertArrayHasKey('api_key', $defaults);
        self::assertArrayHasKey('response_language', $defaults);
        self::assertSame('', $defaults['knowledge_base_id']);
        self::assertSame('', $defaults['ingestion_knowledge_base_id']);
        self::assertSame([], $defaults['allowed_agent_ids']);
        self::assertSame('45', $defaults['request_timeout']);
        self::assertSame('auto', $defaults['response_language']);
    }

    public function test_sanitize_position_invalid(): void {
        self::assertSame(
            'bottom-right',
            \EC_Rag_Options::sanitize(['position' => 'invalid'])['position']
        );
    }

    public function test_sanitize_position_valid(): void {
        self::assertSame(
            'bottom-left',
            \EC_Rag_Options::sanitize(['position' => 'bottom-left'])['position']
        );
    }

    public function test_sanitize_request_timeout_clamps(): void {
        self::assertSame(
            '45',
            \EC_Rag_Options::sanitize(['request_timeout' => '3'])['request_timeout']
        );
        self::assertSame(
            '45',
            \EC_Rag_Options::sanitize(['request_timeout' => '121'])['request_timeout']
        );
        self::assertSame(
            '60',
            \EC_Rag_Options::sanitize(['request_timeout' => '60'])['request_timeout']
        );
    }

    public function test_sanitize_batch_size_clamps(): void {
        self::assertSame(
            '10',
            \EC_Rag_Options::sanitize(['ingestion_batch_size' => '0'])['ingestion_batch_size']
        );
        self::assertSame(
            '10',
            \EC_Rag_Options::sanitize(['ingestion_batch_size' => '51'])['ingestion_batch_size']
        );
        self::assertSame(
            '25',
            \EC_Rag_Options::sanitize(['ingestion_batch_size' => '25'])['ingestion_batch_size']
        );
    }

    public function test_sanitize_colors(): void {
        $result = \EC_Rag_Options::sanitize([
            'primary_color' => '#ff0000',
            'text_color'    => '#00ff00',
        ]);
        self::assertSame('#ff0000', $result['primary_color']);
        self::assertSame('#00ff00', $result['text_color']);
    }

    public function test_sanitize_knowledge_base_id(): void {
        $valid = 'kb_11111111111111111111111111111111';
        self::assertSame(
            $valid,
            \EC_Rag_Options::sanitize(['knowledge_base_id' => $valid])['knowledge_base_id']
        );
        self::assertSame(
            '',
            \EC_Rag_Options::sanitize(['knowledge_base_id' => 'default'])['knowledge_base_id']
        );
        self::assertSame(
            '',
            \EC_Rag_Options::sanitize(['knowledge_base_id' => '../other'])['knowledge_base_id']
        );
    }

    public function test_agent_allowlist_and_default_are_sanitized_together(): void {
        $agent_a = 'agent_' . str_repeat('a', 32);
        $agent_b = 'agent_' . str_repeat('b', 32);
        $result = \EC_Rag_Options::sanitize([
            'allowed_agent_ids' => [$agent_a, '../bad', $agent_a],
            'default_agent_id' => $agent_b,
            'enable_chat_agents_mode' => '1',
        ]);
        self::assertSame([$agent_a], $result['allowed_agent_ids']);
        self::assertSame('', $result['default_agent_id']);
        self::assertSame('1', $result['enable_chat_agents_mode']);
    }

    public function test_get_migrates_legacy_agent_and_ingestion_targets(): void {
        $agent_id = 'agent_' . str_repeat('c', 32);
        $kb_id = 'kb_' . str_repeat('1', 32);
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Options::OPTION_NAME] = [
            'agent_id' => $agent_id,
            'knowledge_base_id' => $kb_id,
        ];
        $options = \EC_Rag_Options::get();
        self::assertSame([$agent_id], $options['allowed_agent_ids']);
        self::assertSame($agent_id, $options['default_agent_id']);
        self::assertSame($kb_id, $options['ingestion_knowledge_base_id']);
    }
}
