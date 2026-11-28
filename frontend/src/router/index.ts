import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'Home',
      component: () => import('../pages/HomePage.vue')
    },
    {
      path: '/create/:taskId?',
      name: 'Create',
      component: () => import('../pages/CreatePage.vue')
    },
    {
      path: '/projects/:projectId',
      name: 'Project',
      component: () => import('../pages/ProjectPage.vue')
    },
    {
      path: '/projects/:projectId/pages/:taskId',
      name: 'ProjectPageEditor',
      component: () => import('../pages/CreatePage.vue')
    },
    {
      path: '/templates-workbench',
      name: 'TemplateWorkbench',
      component: () => import('../pages/TemplateWorkbenchPage.vue')
    },
    {
      path: '/notifications',
      name: 'Notifications',
      component: () => import('../pages/NotificationsPage.vue')
    },
    {
      path: '/materials',
      name: 'Materials',
      component: () => import('../pages/MaterialsPage.vue')
    },
    {
      path: '/account',
      name: 'Account',
      component: () => import('../pages/AccountPage.vue')
    },
    {
      path: '/history',
      name: 'History',
      component: () => import('../pages/HistoryPage.vue')
    }
  ],
  scrollBehavior: () => ({ top: 0 }),
})

export default router
