from datetime import datetime
from importlib import import_module
from random import random

from django.conf import settings
from django.db import transaction

from orchestra.core.errors import AssignmentPolicyError
from orchestra.core.errors import IllegalTaskSubmission
from orchestra.core.errors import ModelSaveError
from orchestra.core.errors import NoTaskAvailable
from orchestra.core.errors import ReviewPolicyError
from orchestra.core.errors import TaskAssignmentError
from orchestra.core.errors import TaskDependencyError
from orchestra.core.errors import TaskStatusError
from orchestra.core.errors import WorkerCertificationError
from orchestra.models import Project
from orchestra.models import Task
from orchestra.models import TaskAssignment
from orchestra.models import Worker
from orchestra.models import WorkerCertification
from orchestra.slack import add_worker_to_project_team
from orchestra.utils.assignment_snapshots import empty_snapshots
from orchestra.utils.notifications import notify_status_change
from orchestra.utils.task_properties import assignment_history
from orchestra.utils.task_properties import current_assignment
from orchestra.utils.task_properties import is_worker_assigned_to_task
from orchestra.workflow import get_workflow_by_slug
from orchestra.workflow import Step

import logging
logger = logging.getLogger(__name__)


def _get_latest_task_data(task):
    active_assignment = (task.assignments
                         .filter(status=TaskAssignment.Status.PROCESSING))
    if active_assignment.exists():
        assignment = active_assignment[0]
    else:
        assignment = (task.assignments.all()
                      .order_by('-assignment_counter').first())
    if not assignment:
        return None
    return assignment.in_progress_task_data


def worker_assigned_to_max_tasks(worker):
    # # TODO(jrbotros): allow per-user exception to task limit
    # return (TaskAssignment.objects
    #         .filter(worker=worker,
    #                 status=TaskAssignment.Status.PROCESSING,
    #                 task__status=Task.Status.PROCESSING)
    #         .count()) >= settings.ORCHESTRA_MAX_IN_PROGRESS_TASKS
    return False


def worker_assigned_to_rejected_task(worker):
    return (TaskAssignment.objects
            .filter(worker=worker,
                    status=TaskAssignment.Status.PROCESSING,
                    task__status=Task.Status.POST_REVIEW_PROCESSING)
            .exists())


def worker_has_reviewer_status(worker,
                               task_class=WorkerCertification.TaskClass.REAL):
    return (WorkerCertification.objects
                               .filter(worker=worker,
                                       role=WorkerCertification.Role.REVIEWER,
                                       task_class=task_class)
                               .exists())


def _worker_certified_for_task(worker, task, role,
                               task_class=WorkerCertification.TaskClass.REAL):
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)

    match_count = (
        WorkerCertification
        .objects
        .filter(worker=worker,
                role=role,
                task_class=task_class,
                certification__slug__in=step.required_certifications)
        .count())
    return len(step.required_certifications) == match_count


def _role_required_to_assign(task):
    """
    Return the role required to assign or reassign a task, and a flag to
    indicate whether the task requires reassignment.
    """
    post_review_role = WorkerCertification.Role.ENTRY_LEVEL
    final_role = WorkerCertification.Role.ENTRY_LEVEL
    assignment_count = task.assignments.count()
    if assignment_count > 2:
        # Role required to reassign post-review processing tasks
        post_review_role = WorkerCertification.Role.REVIEWER
    if assignment_count > 1:
        # Role required to reassign complete and aborted tasks
        final_role = WorkerCertification.Role.REVIEWER

    roles = {
        Task.Status.AWAITING_PROCESSING: WorkerCertification.Role.ENTRY_LEVEL,
        Task.Status.PENDING_REVIEW: WorkerCertification.Role.REVIEWER
    }
    reassigns = {
        Task.Status.PROCESSING: WorkerCertification.Role.ENTRY_LEVEL,
        Task.Status.REVIEWING: WorkerCertification.Role.REVIEWER,
        Task.Status.POST_REVIEW_PROCESSING: post_review_role,
        Task.Status.COMPLETE: final_role
    }

    # If task rejected from higher-level reviewer to a lower-level one, it can
    # only be reassigned to a reviewer
    required_role, needs_reassign = roles.get(task.status, None), False
    if required_role is None:
        required_role, needs_reassign = reassigns.get(task.status, None), True
    if required_role is None:
        raise TaskStatusError('Task status not found.')
    return required_role, needs_reassign


# TODO(jrbotros): make this accept worker_id, task_id instead
@transaction.atomic
def assign_task(worker_id, task_id):
    """
    Return a given task after assigning or reassigning it to the specified
    worker.
    """
    worker = Worker.objects.get(id=worker_id)
    task = Task.objects.get(id=task_id)
    required_role, requires_reassign = _role_required_to_assign(task)
    assignment = current_assignment(task)
    if not _worker_certified_for_task(worker, task, required_role):
        raise WorkerCertificationError('Worker not certified for this task.')
    if is_worker_assigned_to_task(worker, task):
        raise TaskAssignmentError('Worker already assigned to this task.')

    # If task is currently in progress, reassign it
    if requires_reassign:
        assignment.worker = worker
        assignment.save()
        add_worker_to_project_team(worker, task.project)
        return task

    # Otherwise, create new assignment
    assignment_counter = task.assignments.count()
    in_progress_task_data = {}

    if required_role == WorkerCertification.Role.REVIEWER:
        # In-progress task data is the latest
        # submission by a previous worker
        in_progress_task_data = assignment.in_progress_task_data

    previous_status = task.status
    if previous_status == Task.Status.AWAITING_PROCESSING:
        task.status = Task.Status.PROCESSING
    elif previous_status == Task.Status.PENDING_REVIEW:
        task.status = Task.Status.REVIEWING
    else:
        raise TaskAssignmentError('Status incompatible with new assignment')
    task.save()

    (TaskAssignment.objects
        .create(worker=worker,
                task=task,
                status=TaskAssignment.Status.PROCESSING,
                assignment_counter=assignment_counter,
                in_progress_task_data=in_progress_task_data,
                snapshots=empty_snapshots()))

    add_worker_to_project_team(worker, task.project)
    notify_status_change(task, previous_status)
    return task


def get_task_details(task_id):
    """
    Get information about the task
    """
    task = Task.objects.get(id=task_id)
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)
    prerequisites = previously_completed_task_data(task)

    return {
        'workflow': {
            'slug': workflow.slug,
            'name': workflow.name
        },
        'step': {
            'slug': step.slug,
            'name': step.name
        },
        'task_id': task.id,
        'project': {
            'id': task.project.id,
            'details': task.project.short_description,
            'review_document_url': task.project.review_document_url,
            'project_data': task.project.project_data
        },
        'prerequisites': prerequisites
    }


def get_task_assignment_details(task_assignment):
    reviewer_task_assignment = (
        TaskAssignment.objects.filter(
            task=task_assignment.task)
        .order_by('-assignment_counter')[0])
    return {
        'task': {
            'data': task_assignment.in_progress_task_data,
            'status': (dict(Task.STATUS_CHOICES)
                       [task_assignment.task.status])
        },
        'status': (dict(TaskAssignment.STATUS_CHOICES)
                   [task_assignment.status]),
        'is_reviewer': (
            task_assignment.id == reviewer_task_assignment.id and
            task_assignment.assignment_counter > 0),
        'is_read_only': (
            task_assignment.status != TaskAssignment.Status.PROCESSING),
        'work_times_seconds': [
            snapshot['work_time_seconds']
            for snapshot in task_assignment.snapshots['snapshots']]
    }


def get_task_overview_for_worker(task_id, worker):
    task = Task.objects.get(id=task_id)
    if not is_worker_assigned_to_task(worker, task):
        raise TaskAssignmentError('Worker is not associated with task')
    task_details = get_task_details(task_id)

    task_assignment = TaskAssignment.objects.get(worker=worker,
                                                 task=task)
    task_assignment_details = get_task_assignment_details(task_assignment)
    task_assignment_details.update(task_details)
    return task_assignment_details


def tasks_assigned_to_worker(worker):
    """
    Get all the tasks associated with a worker
    """
    valid_task_assignments = TaskAssignment.objects.exclude(
        task__status=Task.Status.ABORTED)

    # get all active task assignments for a user
    active_task_assignments = (
        valid_task_assignments
        .filter(
            worker=worker,
            status=TaskAssignment.Status.PROCESSING)
        .order_by('-task__project__priority',
                  'task__project__start_datetime'))

    inactive_task_assignments = (
        valid_task_assignments
        .filter(
            worker=worker,
            status=TaskAssignment.Status.SUBMITTED
        )
        .exclude(task__status=Task.Status.COMPLETE)
        .order_by('-task__project__priority',
                  'task__project__start_datetime'))

    inactive_processing_task_assignments = []
    inactive_review_task_assignments = []
    for task_assignment in inactive_task_assignments:
        if (
                valid_task_assignments
                .filter(
                    status=TaskAssignment.Status.PROCESSING,
                    task__id=task_assignment.task.id,
                    assignment_counter__lt=task_assignment.assignment_counter)
                .exists()):
            inactive_processing_task_assignments.append(task_assignment)
        else:
            inactive_review_task_assignments.append(task_assignment)

    # TODO(marcua): Do a better job of paginating than cutting off to the most
    # recent 20 tasks.
    complete_task_assignments = (
        valid_task_assignments
        .filter(worker=worker,
                task__status=Task.Status.COMPLETE)
        .order_by('-task__project__priority',
                  '-task__project__start_datetime')[:20])

    task_assignments_overview = {
        'returned': (
            active_task_assignments
            .filter(task__status=Task.Status.POST_REVIEW_PROCESSING)),
        'in_progress': (
            active_task_assignments
            .exclude(task__status=Task.Status.POST_REVIEW_PROCESSING)),
        'pending_review': inactive_review_task_assignments,
        'pending_processing': inactive_processing_task_assignments,
        'complete': complete_task_assignments}

    retval = {}
    for state, task_assignments in iter(task_assignments_overview.items()):
        tasks_val = []
        for task_assignment in task_assignments:
            workflow = get_workflow_by_slug(
                task_assignment.task.project.workflow_slug)
            step = workflow.get_step(
                task_assignment.task.step_slug)
            # TODO(marcua): project should be workflow here, no?
            tasks_val.append({'id': task_assignment.task.id,
                              'step': step.name,
                              'project': workflow.name,
                              'detail':
                              task_assignment.task.project.short_description})
        retval[state] = tasks_val
    return retval


def _is_review_needed(task):
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)

    policy = step.review_policy.get('policy', None)
    sample_rate = step.review_policy.get('rate', None)
    max_reviews = step.review_policy.get('max_reviews', None)

    if (policy == 'sampled_review' and
            sample_rate is not None and
            max_reviews is not None):
        task_assignment_count = task.assignments.all().count()
        if max_reviews <= task_assignment_count - 1:
            return False
        return random() < sample_rate
    elif policy == 'no_review':
        return False
    else:
        raise ReviewPolicyError('Review policy incorrectly specified.')


def get_next_task_status(task, snapshot_type):
    """
    Given current task status and snapshot type provide
    new task status.
    If the second level reviewer rejects a task then
    initial reviewer can not reject it down further down
    and has to fix and submit the task.
    """
    if snapshot_type == TaskAssignment.SnapshotType.SUBMIT:
        if task.status == Task.Status.PROCESSING:
            if _is_review_needed(task):
                return Task.Status.PENDING_REVIEW
            return Task.Status.COMPLETE
        elif task.status == Task.Status.POST_REVIEW_PROCESSING:
            return Task.Status.REVIEWING
        raise IllegalTaskSubmission('Worker can only submit a task.')
    elif snapshot_type == TaskAssignment.SnapshotType.REJECT:
        if task.status == Task.Status.REVIEWING:
            return Task.Status.POST_REVIEW_PROCESSING
        raise IllegalTaskSubmission('Only reviewer can reject the task.')
    elif snapshot_type == TaskAssignment.SnapshotType.ACCEPT:
        if task.status == Task.Status.REVIEWING:
            if _is_review_needed(task):
                return Task.Status.PENDING_REVIEW
            return Task.Status.COMPLETE
        raise IllegalTaskSubmission('Only reviewer can accept the task.')
    raise IllegalTaskSubmission('Unknown task state.')


def _check_worker_allowed_new_assignment(worker, task_status):
    valid_statuses = [Task.Status.AWAITING_PROCESSING,
                      Task.Status.PENDING_REVIEW]
    if task_status not in valid_statuses:
        raise TaskStatusError('Invalid status for new task assignment.')
    elif worker_assigned_to_rejected_task(worker):
        raise TaskAssignmentError('Worker has pending reviewer feedback that '
                                  'must be addressed.')
    elif worker_assigned_to_max_tasks(worker):
        raise TaskAssignmentError('Worker assigned to max number of tasks.')


def get_new_task_assignment(worker, task_status):
    _check_worker_allowed_new_assignment(worker, task_status)

    tasks = (Task.objects
             .filter(status=task_status)
             .exclude(assignments__worker=worker)
             .order_by('-project__priority')
             .order_by('project__start_datetime'))

    certification_error = False
    for task in tasks.iterator():
        try:
            task = assign_task(worker.id, task.id)
            return current_assignment(task)
        except WorkerCertificationError:
            certification_error = True
        except ModelSaveError:
            # Machine task cannot have human worker; treat machine tasks as if
            # they do not exist
            pass

    if certification_error:
        raise WorkerCertificationError
    else:
        raise NoTaskAvailable('No task available for {}'.format(worker))


@transaction.atomic
def save_task(task_id, task_data, worker):
    task = Task.objects.get(id=task_id)
    if not is_worker_assigned_to_task(worker, task):
        raise TaskAssignmentError('Worker is not associated with task')

    # Use select_for_update to prevent concurrency issues with submit_task.
    # See https://github.com/unlimitedlabs/orchestra/issues/2.
    assignment = (TaskAssignment.objects.select_for_update()
                                .get(task=task, worker=worker))

    if assignment.status != TaskAssignment.Status.PROCESSING:
        raise TaskAssignmentError('Worker is not allowed to save')

    assignment.in_progress_task_data = task_data
    assignment.save()


def _are_desired_steps_completed_on_project(desired_steps,
                                            project=None,
                                            completed_tasks=None):
    """
    Determines if `desired_steps` have already been completed on `project`.
    Either `project` or `completed_tasks` will be passed in, since the caller
    sometimes has one but not the other.
    """
    if completed_tasks is None:
        if project is None:
            raise Exception('Must provide either project or completed_tasks')
        completed_tasks = Task.objects.filter(status=Task.Status.COMPLETE,
                                              project=project)
    completed_step_slugs = {task.step_slug for task in completed_tasks}
    return (len({step.slug for step in desired_steps} -
                completed_step_slugs) == 0)


@transaction.atomic
def submit_task(task_id, task_data, snapshot_type, worker, work_time_seconds):
    task = Task.objects.get(id=task_id)

    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)
    if not _are_desired_steps_completed_on_project(step.submission_depends_on,
                                                   project=task.project):
        raise IllegalTaskSubmission('Submission prerequisites are not '
                                    'complete.')

    if task.status == Task.Status.COMPLETE:
        raise TaskStatusError('Task already completed')

    # Use select_for_update to prevent concurrency issues with save_task.
    # See https://github.com/unlimitedlabs/orchestra/issues/2.
    assignments = (TaskAssignment.objects.select_for_update()
                                 .filter(worker=worker, task=task))

    # Worker can belong to only one assignment for a given task.
    if not assignments.count() == 1:
        raise TaskAssignmentError(
            'Task assignment with worker is in broken state.')

    assignment = assignments[0]

    if assignment.status != TaskAssignment.Status.PROCESSING:
        raise IllegalTaskSubmission('Worker is not allowed to submit')

    next_status = get_next_task_status(task, snapshot_type)

    assignment.in_progress_task_data = task_data
    assignment.snapshots['snapshots'].append(
        {'data': assignment.in_progress_task_data,
         'datetime': datetime.utcnow().isoformat(),
         'type': snapshot_type,
         'work_time_seconds': work_time_seconds
         })

    assignment.status = TaskAssignment.Status.SUBMITTED
    assignment.save()
    previous_status = task.status
    task.status = next_status
    task.save()

    if task.status == Task.Status.REVIEWING:
        update_related_assignment_status(task,
                                         assignment.assignment_counter + 1,
                                         assignment.in_progress_task_data)
    elif task.status == Task.Status.POST_REVIEW_PROCESSING:
        update_related_assignment_status(task,
                                         assignment.assignment_counter - 1,
                                         assignment.in_progress_task_data)
    elif task.status == Task.Status.COMPLETE:
        create_subsequent_tasks(task.project)

    notify_status_change(task, previous_status)
    return task


def previously_completed_task_data(task):
    """
    Returns a dict mapping task prerequisites on to their
    latest task assignment information.  The dict is of the form:
    {'previous-slug': {task_assignment_data}, ...}
    """
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)
    prerequisites = {}

    for required_step in step.creation_depends_on:
        required_task = Task.objects.get(step_slug=required_step.slug,
                                         project=task.project)
        if required_task.status != Task.Status.COMPLETE:
            raise TaskDependencyError('Task depenency is not satisfied')

        task_assignment = (required_task.assignments
                           .order_by('-assignment_counter')[0])

        task_details = get_task_details(required_task.id)
        task_assignment_details = get_task_assignment_details(task_assignment)
        task_assignment_details.update(task_details)

        # TODO(kkamalov): check for circular prerequisites
        prerequisites[required_task.step_slug] = task_assignment_details
    return prerequisites


def update_related_assignment_status(task, assignment_counter, data):
    assignment = (TaskAssignment.objects
                  .get(task=task,
                       assignment_counter=assignment_counter))
    assignment.in_progress_task_data = data
    assignment.status = TaskAssignment.Status.PROCESSING
    assignment.save()


def end_project(project_id):
    project = Project.objects.get(id=project_id)
    project.status = Project.Status.ABORTED
    project.save()
    for task in project.tasks.all():
        task.status = Task.Status.ABORTED
        task.save()
        notify_status_change(task, assignment_history(task))


def _preassign_workers(task):
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    step = workflow.get_step(task.step_slug)

    policy = step.assignment_policy.get('policy')
    related_steps = step.assignment_policy.get('steps')

    if step.worker_type == Step.WorkerType.MACHINE:
        if policy:
            raise AssignmentPolicyError('Machine step should not have '
                                        'assignment policy.')
    elif (policy == 'previously_completed_steps'
            and related_steps is not None):
        task = _assign_worker_from_previously_completed_steps(task,
                                                              related_steps)
    elif policy == 'anyone_certified':
        # Leave the task in the awaiting processing pool
        pass
    else:
        raise AssignmentPolicyError('Assignment policy incorrectly specified.')
    return task


def _assign_worker_from_previously_completed_steps(task, related_steps):
    workflow = get_workflow_by_slug(task.project.workflow_slug)
    for slug in related_steps:
        if workflow.get_step(slug).worker_type == Step.WorkerType.MACHINE:
            raise AssignmentPolicyError('Machine step should not be '
                                        'member of assignment policy')
    related_tasks = Task.objects.filter(step_slug__in=related_steps,
                                        project=task.project)
    for related_task in related_tasks:
        entry_level_assignment = assignment_history(related_task).first()
        if entry_level_assignment and entry_level_assignment.worker:
            try:
                return assign_task(entry_level_assignment.worker.id, task.id)
            except:
                # Task could not be assigned to related worker, try with
                # another related worker
                logger.warning('Tried to assign worker %s to step %s, for '
                               'which they are not certified',
                               entry_level_assignment.worker.id,
                               task.step_slug, exc_info=True)
    return task


# TODO(kkamalov): make a periodic job that runs this function periodically
def create_subsequent_tasks(project):
    workflow = get_workflow_by_slug(project.workflow_slug)
    all_step_slugs = workflow.get_step_slugs()

    # get all completed tasks associated with a given project
    completed_tasks = Task.objects.filter(status=Task.Status.COMPLETE,
                                          project=project)
    completed_step_slugs = {task.step_slug for task in completed_tasks}
    for step_slug in all_step_slugs:
        if (step_slug in completed_step_slugs or
            Task.objects.filter(project=project,
                                step_slug=step_slug).exists()):
            continue
        step = workflow.get_step(step_slug)

        if _are_desired_steps_completed_on_project(
                step.creation_depends_on, completed_tasks=completed_tasks):
            # create new task and task_assignment
            task = Task(step_slug=step_slug,
                        project=project,
                        status=Task.Status.AWAITING_PROCESSING)
            task.save()

            _preassign_workers(task)

            if step.worker_type == Step.WorkerType.MACHINE:
                machine_step_scheduler_module = import_module(
                    settings.MACHINE_STEP_SCHEDULER[0])
                machine_step_scheduler_class = getattr(
                    machine_step_scheduler_module,
                    settings.MACHINE_STEP_SCHEDULER[1])

                machine_step_scheduler = machine_step_scheduler_class()
                machine_step_scheduler.schedule(project.id, step_slug)


def task_history_details(task_id):
    task = Task.objects.get(id=task_id)
    return {
        'current_assignment': current_assignment(task),
        'assignment_history': assignment_history(task)
    }
