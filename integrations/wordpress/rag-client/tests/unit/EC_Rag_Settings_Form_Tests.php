<?php
/**
 * EC_Rag_Settings_Form_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class Exposed_EC_Rag_Settings_Form extends \EC_Rag_Settings_Form {

    public static function render_connection(
        array $options,
        array $knowledge_bases,
        bool $catalog_available
    ): string {
        ob_start();
        parent::render_connection_section(
            $options,
            $knowledge_bases,
            $catalog_available
        );
        return (string) ob_get_clean();
    }
}

final class EC_Rag_Settings_Form_Tests extends TestCase {

    public function test_default_choice_is_not_synthesized_when_not_authorized(): void {
        $knowledge_base_id = 'kb_11111111111111111111111111111111';

        $choices = \EC_Rag_Settings_Form::knowledge_base_choices([
            [
                'id'     => $knowledge_base_id,
                'name'   => 'Public articles',
                'status' => 'active',
            ],
        ]);

        self::assertSame(
            [
                [
                    'id'    => $knowledge_base_id,
                    'value' => $knowledge_base_id,
                    'label' => 'Public articles',
                ],
            ],
            $choices
        );
        self::assertNotContains('', array_column($choices, 'value'));
    }

    public function test_choices_include_returned_default_and_filter_unusable_records(): void {
        $active_id = 'kb_22222222222222222222222222222222';
        $deleting_id = 'kb_33333333333333333333333333333333';

        $choices = \EC_Rag_Settings_Form::knowledge_base_choices([
            ['id' => 'default', 'name' => 'Primary', 'status' => 'active'],
            ['id' => $active_id, 'name' => 'Docs', 'status' => 'active'],
            ['id' => $deleting_id, 'name' => 'Deleting', 'status' => 'deleting'],
            ['id' => '../invalid', 'name' => 'Invalid', 'status' => 'active'],
            ['id' => $active_id, 'name' => 'Duplicate', 'status' => 'active'],
        ]);

        self::assertSame(
            [
                ['id' => 'default', 'value' => '', 'label' => 'Primary'],
                ['id' => $active_id, 'value' => $active_id, 'label' => 'Docs'],
            ],
            $choices
        );
    }

    public function test_unavailable_saved_target_is_not_silently_replaced(): void {
        $saved_id = 'kb_11111111111111111111111111111111';
        $authorized_id = 'kb_22222222222222222222222222222222';

        $html = Exposed_EC_Rag_Settings_Form::render_connection(
            [
                'knowledge_base_id' => $saved_id,
                'request_timeout'   => '45',
            ],
            [
                [
                    'id'     => $authorized_id,
                    'name'   => 'Authorized',
                    'status' => 'active',
                ],
            ],
            true
        );

        self::assertMatchesRegularExpression(
            '/value="' . preg_quote($saved_id, '/') . '"\s+selected/s',
            $html
        );
        self::assertDoesNotMatchRegularExpression(
            '/value="' . preg_quote($authorized_id, '/') . '"\s+selected/s',
            $html
        );
        self::assertStringContainsString('Unavailable (' . $saved_id . ')', $html);
    }
}
