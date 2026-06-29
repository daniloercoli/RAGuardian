<?php
/**
 * Plugin Name: Raguardian
 * Description: Server-side WordPress client for a RAGuardian user/workspace API key.
 * Version: 0.5.2
 * Author: Danilo Ercoli
 * License: MIT
 * License URI: https://opensource.org/license/mit/
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

// ---------- Constants ----------

define('EC_RAG_PLUGIN_FILE', __FILE__);
define('EC_RAG_PLUGIN_DIR', plugin_dir_path(__FILE__));
define('EC_RAG_INCLUDES', EC_RAG_PLUGIN_DIR . 'includes/');

// ---------- Bootstrap ----------

require_once EC_RAG_INCLUDES . 'version.php';
require_once EC_RAG_INCLUDES . 'autoload.php';

// Register all components.
EC_Rag_Options::register();
EC_Rag_Widget::register();
EC_Rag_Ingestion::register();
EC_Rag_Ajax::register();
EC_Rag_Health_Check::register();

register_deactivation_hook(__FILE__, [EC_Rag_Health_Check::class, 'deactivate']);
