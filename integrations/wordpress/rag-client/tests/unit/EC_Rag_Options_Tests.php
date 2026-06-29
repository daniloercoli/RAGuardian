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
}
