<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">实时流数据湖构建与治理平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向实时流数据接入、Delta湖表构建、流批一体转换、分区合并和数据治理的流式数据湖平台</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "流数据源", path: "/stream_source" }, { label: "湖表定义", path: "/lake_table" }, { label: "流式摄入", path: "/stream_ingest" }, { label: "提交版本", path: "/delta_commit" }, { label: "分区合并", path: "/partition_merge" }, { label: "流Schema演进", path: "/stream_schema" }, { label: "快照管理", path: "/snapshot_mgr" }, { label: "快照差异审计", path: "/snapshot-audit" }, { label: "流质量校验", path: "/stream_quality" }, { label: "历史清理", path: "/vacuum_job" }, { label: "批流合并", path: "/batch_merge" }, { label: "湖表血缘", path: "/lake_lineage" }, { label: "保留策略", path: "/retention_policy" }, { label: "压缩任务", path: "/compaction_task" }, { label: "数据回填", path: "/backfill_pipeline" }, { label: "流处理指标", path: "/stream_metric" }, { label: "格式迁移", path: "/iceberg_migrate" }, { label: "表目录同步", path: "/table_catalog" }, { label: "湖仓审计", path: "/lake_audit" }]
</script>
