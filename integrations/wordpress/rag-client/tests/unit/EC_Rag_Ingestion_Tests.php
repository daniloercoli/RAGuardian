<?php
/**
 * EC_Rag_Ingestion_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Ingestion_Tests extends TestCase {

    private string $queue_path = '';

    protected function setUp(): void {
        $GLOBALS['ec_rag_test_options'] = [];
        $GLOBALS['ec_rag_test_scheduled_events'] = [];
        $GLOBALS['ec_rag_test_http_handler'] = static function () {
            return [
                'response' => ['code' => 202],
                'body'     => '{"status":"queued"}',
                'headers'  => [],
            ];
        };
    }

    protected function tearDown(): void {
        if ($this->queue_path !== '' && is_file($this->queue_path)) {
            unlink($this->queue_path);
        }
    }

    public function test_import_reads_one_streamed_batch_and_deletes_completed_queue(): void {
        $queue_path = tempnam(sys_get_temp_dir(), 'ec-rag-queue-');
        self::assertNotFalse($queue_path);
        $this->queue_path = $queue_path;

        $articles = [];
        for ($post_id = 1; $post_id <= 3; $post_id++) {
            $articles[] = json_encode([
                'post_id'      => $post_id,
                'title'        => 'Post ' . $post_id,
                'link'         => 'https://example.test/post-' . $post_id,
                'published_at' => '2026-07-23',
                'excerpt'      => '',
                'content'      => 'Content ' . $post_id,
            ]);
        }
        file_put_contents($this->queue_path, implode("\n", $articles) . "\n");

        $options = \EC_Rag_Options::defaults();
        $options['base_url'] = 'https://rag.example.test';
        $options['api_key'] = 'test-key';
        $options['ingestion_batch_size'] = '1';
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Options::OPTION_NAME] = $options;
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Ingestion::IMPORT_OPTION] = [
            'status'       => 'queued',
            'queue_path'   => $this->queue_path,
            'queue_offset' => 0,
            'total'        => 3,
            'processed'    => 0,
            'succeeded'    => 0,
            'failed'       => 0,
            'errors'       => [],
        ];

        self::assertTrue(\EC_Rag_Ingestion::process_import_batch());
        $state = \EC_Rag_Ingestion::get_import_state();
        self::assertSame(1, $state['processed']);
        self::assertSame('queued', $state['status']);
        self::assertGreaterThan(0, $state['queue_offset']);
        self::assertFileExists($this->queue_path);

        self::assertTrue(\EC_Rag_Ingestion::process_import_batch());
        self::assertSame(2, \EC_Rag_Ingestion::get_import_state()['processed']);

        self::assertTrue(\EC_Rag_Ingestion::process_import_batch());
        $state = \EC_Rag_Ingestion::get_import_state();
        self::assertSame(3, $state['processed']);
        self::assertSame(3, $state['succeeded']);
        self::assertSame('completed', $state['status']);
        self::assertSame('', $state['queue_path']);
        self::assertFileDoesNotExist($this->queue_path);
    }

    public function test_scheduled_post_sync_freezes_the_selected_knowledge_base(): void {
        $knowledge_base_id = 'kb_11111111111111111111111111111111';
        $options = \EC_Rag_Options::defaults();
        $options['knowledge_base_id'] = $knowledge_base_id;
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Options::OPTION_NAME] = $options;

        \EC_Rag_Ingestion::schedule_post_sync(42);

        self::assertSame(
            [42, 0, $knowledge_base_id],
            $GLOBALS['ec_rag_test_scheduled_events'][0]['args']
        );
    }

    public function test_import_batch_uses_frozen_target_after_settings_change(): void {
        $queue_path = tempnam(sys_get_temp_dir(), 'ec-rag-queue-');
        self::assertNotFalse($queue_path);
        $this->queue_path = $queue_path;
        file_put_contents(
            $queue_path,
            json_encode([
                'post_id' => 7,
                'title' => 'Frozen target',
                'link' => 'https://example.test/frozen',
                'published_at' => '2026-07-28',
                'excerpt' => '',
                'content' => 'Content',
            ]) . "\n"
        );
        $frozen_id = 'kb_11111111111111111111111111111111';
        $current_id = 'kb_22222222222222222222222222222222';
        $options = \EC_Rag_Options::defaults();
        $options['base_url'] = 'https://rag.example.test';
        $options['api_key'] = 'test-key';
        $options['knowledge_base_id'] = $current_id;
        $options['ingestion_batch_size'] = '1';
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Options::OPTION_NAME] = $options;
        $GLOBALS['ec_rag_test_options'][\EC_Rag_Ingestion::IMPORT_OPTION] = [
            'status' => 'queued',
            'queue_path' => $queue_path,
            'queue_offset' => 0,
            'total' => 1,
            'processed' => 0,
            'succeeded' => 0,
            'failed' => 0,
            'errors' => [],
            'knowledge_base_id' => $frozen_id,
        ];
        $GLOBALS['ec_rag_test_http_handler'] = function ($method, $url, $args) use ($frozen_id, $current_id) {
            self::assertStringContainsString(
                'name="knowledge_base_id"' . "\r\n\r\n" . $frozen_id,
                $args['body']
            );
            self::assertStringNotContainsString($current_id, $args['body']);
            return [
                'response' => ['code' => 202],
                'body' => '{"status":"queued"}',
                'headers' => [],
            ];
        };

        self::assertTrue(\EC_Rag_Ingestion::process_import_batch());
        self::assertSame('completed', \EC_Rag_Ingestion::get_import_state()['status']);
    }
}
